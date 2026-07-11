"""trafficflow.worker — standalone AI inference worker process.

Runs as a long-lived process inside the Docker worker container.
Reads camera/job configuration from environment variables, then runs
TrafficFlowPipeline in a loop (or once for file-based sources).

Supports graceful shutdown via SIGTERM/SIGINT — the worker stops all
pipelines and exits cleanly within the shutdown timeout.

Usage:
    python -m trafficflow.worker

Environment variables (see deploy/stack/.env.example):
    APP_CONFIG          path to app.yaml
    CAMERAS_DIR         directory of per-camera YAML configs
    MODEL_FILE          YOLO weights filename (inside WEIGHTS_DIR)
    WEIGHTS_DIR         directory containing model weights
    OUTPUT_DIR          root directory for pipeline outputs
    STORAGE_ROOT        root directory for tiered storage
    DETECT_EVERY_N_FRAMES  frame-skip ratio (default 1)
    HALF_PRECISION      "true" to enable FP16 (GPU only)
    REDIS_HOST / REDIS_PORT  Redis connection (optional)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from pathlib import Path

import yaml

from tf_common.safe_path import safe_join, validate_identifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s \u2014 %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("trafficflow.worker")


def _build_pipeline_config(camera_cfg: dict, app_cfg: dict) -> dict:
    """Merge app-level defaults with per-camera overrides into a pipeline config."""
    # Camera files are compiled by ``tf_core.config.compiler`` into the
    # pipeline schema. Overlay every compiled section here; copying only
    # input/output used to silently drop detector/tracking/lanes and made the
    # worker fail at startup with "Missing 'tracking' section".
    cfg = {**app_cfg, **camera_cfg}

    cfg.setdefault("detector", {})
    weights_dir = Path(os.getenv("WEIGHTS_DIR", "weights"))
    model_file = os.getenv("MODEL_FILE", cfg.get("detector", {}).get("weights", "yolo11n.pt"))
    model_path = Path(model_file)
    if (
        not model_path.is_absolute()
        and model_path.parts[:1] != (weights_dir.name,)
    ):
        model_path = weights_dir / model_path
    cfg["detector"]["weights"] = str(model_path)
    cfg["detector"]["half"] = os.getenv("HALF_PRECISION", "false").lower() == "true"
    cfg["detector"].setdefault("detect_every_n_frames",
                               int(os.getenv("DETECT_EVERY_N_FRAMES", 1)))

    server_cfg = camera_cfg.get("server", {})
    cfg["camera_id"] = camera_cfg.get("camera_id") or server_cfg.get("camera_id", "unknown")
    cfg["server"] = {
        **server_cfg,
        "job_id": camera_cfg.get("job_id") or server_cfg.get("job_id", "live"),
    }

    is_live = str(camera_cfg.get("source", camera_cfg.get("input", {}).get("source", ""))).startswith("rtsp") or \
              str(camera_cfg.get("source_type", camera_cfg.get("input", {}).get("source_type", ""))) == "youtube_live"
    if is_live:
        cfg.setdefault("output", {})
        cfg["output"]["save_jsonl"] = False
        cfg["output"]["save_csv"] = False

    # Lane / line / frame_size come from the camera config
    for key in ("lanes", "counting_lines", "frame_size", "input", "output"):
        if key in camera_cfg:
            cfg[key] = camera_cfg[key]

    cfg.setdefault("redis", {
        "enabled": bool(os.getenv("REDIS_HOST")),
        "host": os.getenv("REDIS_HOST", "localhost"),
        "port": int(os.getenv("REDIS_PORT", 6379)),
    })

    cfg.setdefault("storage", {
        "storage_root": os.getenv("STORAGE_ROOT", "data/storage"),
    })

    return cfg


def _load_camera_configs() -> list[dict]:
    """Load all YAML files from CAMERAS_DIR."""
    cameras_dir = Path(os.getenv("CAMERAS_DIR", "configs/cameras"))
    if not cameras_dir.is_dir():
        logger.error("CAMERAS_DIR not found: %s", cameras_dir)
        return []

    from tf_core.config.compiler import compile_camera_config
    from tf_core.config.loader import normalize_camera_config
    configs = []
    for yaml_file in sorted(cameras_dir.glob("*.yaml")):
        try:
            # Validate and compile the same source-of-truth schema used by
            # API-created jobs, so live worker execution cannot drift from
            # the job pipeline configuration.
            cfg = compile_camera_config(yaml_file, job_id="live")
            cfg = normalize_camera_config(cfg)
            server_cfg = cfg.get("server", {})
            cfg.setdefault("camera_id", server_cfg.get("camera_id"))
            cfg.setdefault("source", server_cfg.get("source"))
            cfg.setdefault("source_type", server_cfg.get("source_type"))
            cfg.setdefault("_path", str(yaml_file))
            configs.append(cfg)
            logger.info("Loaded camera config: %s", yaml_file.name)
        except (yaml.YAMLError, OSError):
            logger.warning("Failed to load %s", yaml_file, exc_info=True)
    return configs


def _load_app_config() -> dict:
    from tf_core.config.loader import load_yaml
    app_config_path = Path(os.getenv("APP_CONFIG", "configs/app.yaml"))
    if app_config_path.exists():
        return load_yaml(app_config_path)
    return {}


def run_camera(camera_cfg: dict, app_cfg: dict) -> None:
    """Run the pipeline for a single camera, restarting on error."""
    from tf_worker.pipeline import TrafficFlowPipeline

    pipeline_cfg = _build_pipeline_config(camera_cfg, app_cfg)
    source = camera_cfg.get("source") or camera_cfg.get("input", {}).get("source")
    if not source:
        logger.error("No source defined in camera config: %s", camera_cfg.get("_path"))
        return

    camera_id = validate_identifier(camera_cfg.get("camera_id", "unknown"), "camera_id")
    output_dir = safe_join(Path(os.getenv("OUTPUT_DIR", "data/outputs")), camera_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    import tempfile

    import yaml as pyyaml
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        pyyaml.dump(pipeline_cfg, f)
        tmp_cfg_path = f.name

    try:
        adapter_factory = None
        if os.getenv("DATABASE_URL"):
            def adapter_factory():
                from tf_api.storage_adapters import make_server_adapters
                from tf_db.session import SessionLocal
                return make_server_adapters(SessionLocal())

        pipeline = TrafficFlowPipeline(
            config_path=tmp_cfg_path,
            output_dir=output_dir,
            source=source,
            adapter_factory=adapter_factory,
        )
        pipeline.run()
    finally:
        Path(tmp_cfg_path).unlink(missing_ok=True)


_stop_event = threading.Event()


def _heartbeat_loop(camera_count: int) -> None:
    """Publish a short-lived worker heartbeat for the API health endpoint."""
    redis_host = os.getenv("REDIS_HOST")
    if not redis_host:
        return
    try:
        import redis

        client = redis.Redis(
            host=redis_host,
            port=int(os.getenv("REDIS_PORT", "6379")),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        while not _stop_event.is_set():
            client.set(
                "trafficflow:worker:heartbeat",
                str(camera_count),
                ex=30,
            )
            _stop_event.wait(timeout=10)
    except Exception:
        logger.warning("Worker heartbeat unavailable", exc_info=True)


def _handle_sigterm(signum: int, _frame) -> None:
    """Signal handler for SIGTERM/SIGINT — signals all loops to stop."""
    sig_name = signal.Signals(signum).name
    logger.info("Received %s — shutting down gracefully ...", sig_name)
    _stop_event.set()


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    app_cfg = _load_app_config()
    camera_configs = _load_camera_configs()

    if not camera_configs:
        logger.error("No camera configs found \u2014 exiting.")
        sys.exit(1)

    logger.info("Starting TrafficFlow worker with %d camera(s)", len(camera_configs))

    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(len(camera_configs),),
        name="worker-heartbeat",
        daemon=True,
    )
    heartbeat.start()

    def _is_live(cam: dict) -> bool:
        source = str(cam.get("source", cam.get("input", {}).get("source", "")))
        source_type = str(cam.get("source_type", cam.get("input", {}).get("source_type", "")))
        return source.startswith("rtsp") or source_type == "youtube_live"

    if len(camera_configs) == 1:
        cam = camera_configs[0]
        live = _is_live(cam)
        while not _stop_event.is_set():
            try:
                run_camera(cam, app_cfg)
            except (OSError, ValueError, RuntimeError):
                logger.exception("Pipeline error for camera %s", cam.get("camera_id"))
            if not live:
                break
            if _stop_event.is_set():
                break
            logger.info("Restarting pipeline in 5 s \u2026")
            _stop_event.wait(timeout=5)
    else:
        threads = []
        for cam in camera_configs:
            def _run(c=cam):
                live = _is_live(c)
                while not _stop_event.is_set():
                    try:
                        run_camera(c, app_cfg)
                    except (OSError, ValueError, RuntimeError):
                        logger.exception("Pipeline error for camera %s", c.get("camera_id"))
                    if not live:
                        break
                    if _stop_event.is_set():
                        break
                    _stop_event.wait(timeout=5)

            t = threading.Thread(target=_run, name=f"worker-{cam.get('camera_id', 'unknown')}", daemon=False)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=30)
            if t.is_alive():
                logger.warning("Thread %s did not exit — continuing", t.name)

    logger.info("Worker shut down complete.")


if __name__ == "__main__":
    main()
