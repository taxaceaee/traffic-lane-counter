"""trafficflow.worker — standalone AI inference worker process.

Runs as a long-lived process inside the Docker worker container.
Reads camera/job configuration from environment variables, then runs
TrafficFlowPipeline in a loop (or once for file-based sources).

Supports graceful shutdown via SIGTERM/SIGINT — the worker stops all
pipelines and exits cleanly within the shutdown timeout.

Usage:
    python -m trafficflow.worker

Environment variables (see deploy/.env.example):
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

from backend.io.safe_path import safe_join, validate_identifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s \u2014 %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("trafficflow.worker")


def _build_pipeline_config(camera_cfg: dict, app_cfg: dict) -> dict:
    """Merge app-level defaults with per-camera overrides into a pipeline config."""
    cfg = {**app_cfg}

    cfg.setdefault("detector", {})
    weights_dir = Path(os.getenv("WEIGHTS_DIR", "weights"))
    model_file = os.getenv("MODEL_FILE", cfg.get("detector", {}).get("weights", "yolo11s.pt"))
    cfg["detector"]["weights"] = str(weights_dir / model_file)
    cfg["detector"]["half"] = os.getenv("HALF_PRECISION", "false").lower() == "true"
    cfg["detector"].setdefault("detect_every_n_frames",
                               int(os.getenv("DETECT_EVERY_N_FRAMES", 1)))

    cfg["camera_id"] = camera_cfg.get("camera_id", "unknown")
    cfg["server"] = {"job_id": camera_cfg.get("job_id", "live")}

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
        "storage_root": os.getenv("STORAGE_ROOT", "storage"),
    })

    return cfg


def _load_camera_configs() -> list[dict]:
    """Load all YAML files from CAMERAS_DIR."""
    cameras_dir = Path(os.getenv("CAMERAS_DIR", "configs/cameras"))
    if not cameras_dir.is_dir():
        logger.error("CAMERAS_DIR not found: %s", cameras_dir)
        return []

    from shared.config.loader import load_yaml, normalize_camera_config
    configs = []
    for yaml_file in sorted(cameras_dir.glob("*.yaml")):
        try:
            cfg = normalize_camera_config(load_yaml(yaml_file))
            cfg.setdefault("_path", str(yaml_file))
            configs.append(cfg)
            logger.info("Loaded camera config: %s", yaml_file.name)
        except (yaml.YAMLError, OSError):
            logger.warning("Failed to load %s", yaml_file, exc_info=True)
    return configs


def _load_app_config() -> dict:
    from shared.config.loader import load_yaml
    app_config_path = Path(os.getenv("APP_CONFIG", "configs/app.yaml"))
    if app_config_path.exists():
        return load_yaml(app_config_path)
    return {}


def run_camera(camera_cfg: dict, app_cfg: dict) -> None:
    """Run the pipeline for a single camera, restarting on error."""
    from backend.pipeline import TrafficFlowPipeline

    pipeline_cfg = _build_pipeline_config(camera_cfg, app_cfg)
    source = camera_cfg.get("source") or camera_cfg.get("input", {}).get("source")
    if not source:
        logger.error("No source defined in camera config: %s", camera_cfg.get("_path"))
        return

    camera_id = validate_identifier(camera_cfg.get("camera_id", "unknown"), "camera_id")
    output_dir = safe_join(Path(os.getenv("OUTPUT_DIR", "outputs")), camera_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    import tempfile

    import yaml as pyyaml
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        pyyaml.dump(pipeline_cfg, f)
        tmp_cfg_path = f.name

    try:
        pipeline = TrafficFlowPipeline(
            config_path=tmp_cfg_path,
            output_dir=output_dir,
            source=source,
        )
        pipeline.run()
    finally:
        Path(tmp_cfg_path).unlink(missing_ok=True)


_stop_event = threading.Event()


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
