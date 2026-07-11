"""Live stream API — MJPEG endpoint for annotated camera frames.

24/7 operational hardening:
- Camera auto-reconnection with exponential backoff
- Thread-safe stream registry
- Resource limits: max cameras, stale stream cleanup
- Frontend output FPS is capped to the requested stream rate
- Real-time DB persistence: crossing events stored to DB via StorageWorker
"""

import logging
import os
import secrets
import threading
import time
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

import cv2
import numpy as np
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.responses import StreamingResponse

from tf_api.api.routes_auth import decode_access_token, get_current_user, require_operator
from tf_api.services.settings_service import (
    get_detection_defaults,
    get_max_streams,
    get_preview_defaults,
)
from tf_common.safe_path import validate_identifier
from tf_common.live_errors import diagnose_stream_error
from tf_common.viz_colors import build_lane_color_map, color_for_lane, color_for_track
from tf_core.config.loader import normalize_camera_config
from tf_core.detection_core import DetectionCore
from tf_core.roi import CropROI

# Try to import MotionDetector — lives in backend.detection and can save
# 40-60 % GPU on static-scene cameras (night / empty parking).
try:
    from tf_worker.detection.motion_detector import MotionDetector as _MotionDetector
except ImportError:
    _MotionDetector = None

logger = logging.getLogger("trafficflow.live_api")

router = APIRouter(tags=["live"])
security = HTTPBearer(auto_error=False)

_MAX_STREAMS = 16
# How often to scan for idle pipelines (no MJPEG viewers).
_STREAM_CLEANUP_INTERVAL = 10  # seconds
# Tear down *viewer-only* pipelines after no MJPEG consumers.
# always_on pipelines (auto-started) are never cleaned by this path.
_STREAM_IDLE_TTL_SEC = 20.0
_MAX_RECONNECT_ATTEMPTS = 10
_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_MAX = 60.0
_SUPERVISOR_INTERVAL_SEC = 30.0
_supervisor_stop: threading.Event | None = None
_supervisor_thread: threading.Thread | None = None


def _env_auto_start_live() -> bool:
    """When true, all camera YAMLs run detection continuously after API boot."""
    return os.getenv("AUTO_START_LIVE_STREAMS", "true").strip().lower() not in {
        "0", "false", "no", "off",
    }

# Each stream holds: (DetectionCore, Queue, meta_dict, StorageWorker, session, stop_event)
_streams: dict[str, tuple[DetectionCore, Queue, dict[str, Any], Any, Any, threading.Event]] = {}
_last_snapshots: dict[str, tuple[bytes, int, int, float]] = {}

# Per-camera timing accumulator for pipeline component profiling.
_perf_timings: dict[str, dict[str, float]] = {}
_lock = threading.Lock()
_last_cleanup = time.monotonic()
_SNAPSHOT_REFRESH_SEC = 2.0
_STREAM_TICKET_TTL = 60.0
_stream_tickets: dict[str, tuple[str, float]] = {}


def _log_timing(camera_id: str, label: str, duration_ms: float) -> None:
    """Accumulate pipeline timings that DetectionCore itself does not own."""
    _perf_timings.setdefault(camera_id, {})[label] = duration_ms


def _update_snapshot(camera_id: str, frame: np.ndarray) -> None:
    now = time.monotonic()
    cached = _last_snapshots.get(camera_id)
    if cached is not None and (now - cached[3]) < _SNAPSHOT_REFRESH_SEC:
        return
    height, width = frame.shape[:2]
    success, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
    if success:
        _last_snapshots[camera_id] = (jpeg.tobytes(), width, height, now)


def _restore_original_bboxes(det: dict[str, Any], roi: CropROI | None, width: int, height: int) -> None:
    if roi is None:
        return
    for collection_name in ("tracks", "raw_detections", "frame_tracks"):
        for item in det.get(collection_name, []):
            if "bbox" in item:
                item["bbox"] = roi.to_original(item["bbox"], width, height)


def _cleanup_stale_streams(force: bool = False) -> None:
    """Remove viewer-only streams with no MJPEG consumers.

    Pipelines marked ``always_on`` (auto-started at boot) keep running so
    Dashboard / Events / counts keep receiving realtime data without a browser
    opening Live Monitoring.
    """
    global _last_cleanup
    now = time.monotonic()
    if not force and now - _last_cleanup < _STREAM_CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    with _lock:
        dead = [
            cid
            for cid, (_core, _q, meta, sw, sess, stop_ev) in _streams.items()
            if not meta.get("always_on")
            and meta.get("connections", 0) <= 0
            and now - meta.get("last_access", 0) > _STREAM_IDLE_TTL_SEC
        ]
    for cid in dead:
        if _cleanup_stream(cid):
            logger.info("Cleaned up idle stream (no viewers): %s", cid)


def ensure_live_pipeline(camera_id: str, *, always_on: bool = False) -> bool:
    """Ensure a live detection pipeline is running for *camera_id*.

    Returns True if the pipeline is (or becomes) registered in ``_streams``.
    Safe to call repeatedly from autostart / supervisor / MJPEG handlers.
    """
    validate_identifier(camera_id, name="camera_id")
    with _lock:
        if camera_id in _streams:
            meta = _streams[camera_id][2]
            if always_on:
                meta["always_on"] = True
            meta["last_access"] = time.monotonic()
            return True
        max_streams = max(1, min(get_max_streams() or _MAX_STREAMS, 64))
        if len(_streams) >= max_streams:
            logger.warning(
                "Cannot start %s: max concurrent streams reached (%d)",
                camera_id, max_streams,
            )
            return False

    cfg = _load_camera_config(camera_id)
    if cfg is None:
        logger.warning("Cannot start live pipeline: camera config missing for %s", camera_id)
        return False

    try:
        core, queue, stream_meta, sw, sess, stop_ev = _start_pipeline(camera_id, cfg)
    except Exception:
        logger.exception("Failed to start live pipeline for %s", camera_id)
        try:
            from tf_common.monitoring.live_metrics import record_stream_state
            record_stream_state(camera_id, "error", "Pipeline start failed")
        except Exception:
            pass
        return False

    stream_meta["always_on"] = bool(always_on)
    stream_meta["connections"] = int(stream_meta.get("connections", 0) or 0)
    stream_meta["last_access"] = time.monotonic()

    with _lock:
        if camera_id in _streams:
            # Another starter won the race — stop the duplicate we just created.
            stop_ev.set()
            try:
                sw.stop(timeout=1.0)
            except Exception:
                pass
            return True
        _streams[camera_id] = (core, queue, stream_meta, sw, sess, stop_ev)

    logger.info(
        "Live pipeline started for %s (always_on=%s)",
        camera_id, always_on,
    )
    return True


def list_configured_camera_ids() -> list[str]:
    cam_dir = Path("configs/cameras")
    if not cam_dir.exists():
        return []
    return sorted(p.stem for p in cam_dir.glob("*.yaml"))


def start_all_live_pipelines(*, stagger_sec: float = 1.5) -> dict[str, bool]:
    """Start always-on detection for every camera YAML (staggered)."""
    results: dict[str, bool] = {}
    ids = list_configured_camera_ids()
    if not ids:
        logger.warning("No camera configs found under configs/cameras/")
        return results
    logger.info("Auto-starting %d live pipeline(s)...", len(ids))
    for i, camera_id in enumerate(ids):
        results[camera_id] = ensure_live_pipeline(camera_id, always_on=True)
        if i + 1 < len(ids) and stagger_sec > 0:
            time.sleep(stagger_sec)
    ok = sum(1 for v in results.values() if v)
    logger.info("Auto-start complete: %d/%d pipelines running", ok, len(ids))
    return results


def start_live_supervisor() -> None:
    """Background loop: re-start always-on cameras that died."""
    global _supervisor_stop, _supervisor_thread
    if not _env_auto_start_live():
        logger.info("AUTO_START_LIVE_STREAMS disabled — supervisor not started")
        return
    if _supervisor_thread is not None and _supervisor_thread.is_alive():
        return
    _supervisor_stop = threading.Event()

    def _loop() -> None:
        assert _supervisor_stop is not None
        # Initial boot: start everything once (blocking in this thread).
        try:
            start_all_live_pipelines(stagger_sec=1.5)
        except Exception:
            logger.exception("Initial live auto-start failed")
        while not _supervisor_stop.wait(_SUPERVISOR_INTERVAL_SEC):
            if not _env_auto_start_live():
                continue
            for camera_id in list_configured_camera_ids():
                try:
                    with _lock:
                        running = camera_id in _streams
                    if not running:
                        logger.warning("Supervisor restarting missing pipeline: %s", camera_id)
                        ensure_live_pipeline(camera_id, always_on=True)
                    else:
                        # Heartbeat always_on streams so incidental cleaners leave them alone.
                        with _lock:
                            stream = _streams.get(camera_id)
                            if stream is not None:
                                meta = stream[2]
                                if meta.get("always_on"):
                                    meta["last_access"] = time.monotonic()
                except Exception:
                    logger.exception("Supervisor failed for %s", camera_id)

    _supervisor_thread = threading.Thread(
        target=_loop, daemon=True, name="live-supervisor",
    )
    _supervisor_thread.start()
    logger.info("Live pipeline supervisor started")


def stop_live_supervisor() -> None:
    global _supervisor_stop, _supervisor_thread
    if _supervisor_stop is not None:
        _supervisor_stop.set()
    if _supervisor_thread is not None:
        _supervisor_thread.join(timeout=5.0)
    _supervisor_thread = None
    _supervisor_stop = None


def _load_camera_config(camera_id: str) -> dict[str, Any] | None:
    config_path = Path(f"configs/cameras/{camera_id}.yaml")
    if not config_path.exists():
        return None
    with open(config_path, encoding="utf-8") as f:
        return normalize_camera_config(yaml.safe_load(f))


def _normalize_lane(lane: dict[str, Any]) -> dict[str, Any]:
    """Normalise lane keys: ensure 'id' is present (from 'lane_id' or 'id')."""
    normalized = dict(lane)
    if "id" not in normalized and "lane_id" in normalized:
        normalized["id"] = normalized.pop("lane_id")
    if "points" not in normalized and "polygon" in normalized:
        normalized["points"] = normalized.pop("polygon")
    return normalized


def _load_lanes(cfg: dict[str, Any], camera_cfg_path: Path | None = None) -> list[dict[str, Any]]:
    lane_cfg = cfg.get("lanes", {})
    if isinstance(lane_cfg, dict) and "config_path" in lane_cfg:
        lanes_path = Path(lane_cfg["config_path"])
        if not lanes_path.is_absolute():
            # Resolve relative to the camera config file's directory
            base = camera_cfg_path.parent if camera_cfg_path else Path("configs/cameras")
            lanes_path = (base / lanes_path).resolve()
        with open(lanes_path, encoding="utf-8") as f:
            lanes_data = yaml.safe_load(f)
        return [_normalize_lane(lane) for lane in lanes_data.get("lanes", [])]
    if isinstance(lane_cfg, list):
        return [_normalize_lane(lane) for lane in lane_cfg if isinstance(lane, dict)]
    return []


def _load_zones(cfg: dict[str, Any]) -> list[list[list[float]]]:
    """Load detection zone polygons from configs/detection_zones/{camera_id}_zones.yaml.

    Returns a list of polygons, where each polygon is a list of [x, y] points.
    Returns empty list if no zones file exists.
    """
    camera_id = cfg.get("camera_id") or cfg.get("source", {}).get("camera_id", "")
    if not camera_id:
        return []
    zones_path = Path("configs/detection_zones") / f"{camera_id}_zones.yaml"
    if not zones_path.exists():
        return []
    try:
        with open(zones_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return [zone["polygon"] for zone in raw.get("zones", [])
                if isinstance(zone.get("polygon"), list) and len(zone["polygon"]) >= 3]
    except Exception:
        logger.warning("Failed to load zones for %s", camera_id, exc_info=True)
        return []


def _resolve_model_weights(model_section: dict[str, Any]) -> dict[str, Any]:
    """Resolve model settings from camera YAML ``model:`` section.

    Looks up ``model_id`` in ``configs/models.yaml`` to obtain the actual
    weights path, class mode, and builds the ``detector`` sub-dict that
    ``DetectionCore`` expects.
    """
    defaults = get_detection_defaults()
    model_id = model_section.get("model_id", "yolo11n_coco")
    # Resolve weights from models registry
    weights = "weights/yolo11n.pt"
    class_mode = "coco_pretrained"
    models_path = Path("configs/models.yaml")
    if models_path.exists():
        with open(models_path, encoding="utf-8") as _f:
            _models_raw = yaml.safe_load(_f) or {}
        for _m in _models_raw.get("models", []):
            if _m.get("model_id") == model_id:
                weights = _m.get("path", weights)
                class_mode = _m.get("class_mode", class_mode)
                break

    return {
        "weights": weights,
        # imgsz may be raised later to native ROI / full-frame size.
        "imgsz": model_section.get("imgsz", defaults.get("imgsz", 1280)),
        "conf": model_section.get("conf_threshold", defaults.get("confidence", 0.25)),
        "iou": model_section.get("iou_threshold", defaults.get("iou", 0.45)),
        "class_mode": class_mode,
        "allowed_classes": model_section.get("allowed_classes", []),
        "half": model_section.get("half", defaults.get("half", True)),
        # Detect every frame by default — max object recall for live traffic.
        "detect_every_n_frames": model_section.get(
            "detect_every_n_frames",
            defaults.get("detect_every_n_frames", 1),
        ),
        "roi_crop": model_section.get("roi_crop", defaults.get("roi_crop", True)),
        "max_detections": model_section.get(
            "max_detections", defaults.get("max_detections", 500)
        ),
        "roi_padding": model_section.get(
            "roi_padding", defaults.get("roi_padding", 80)
        ),
    }


def _cfg_to_pipeline_dict(cfg: dict[str, Any], camera_cfg_path: Path | None = None) -> dict[str, Any]:
    detector = _resolve_model_weights(cfg.get("model", {}))
    lanes = _load_lanes(cfg, camera_cfg_path)
    frame = cfg.get("frame_size", {})
    tracking = cfg.get("tracking", {})
    counting = cfg.get("counting", {})

    return {
        "frame_size": {
            "width": frame.get("width", 960),
            "height": frame.get("height", 540),
        },
        "coordinate_space": "original_frame",
        "detector": detector,
        "class_modes": {
            detector.get("class_mode", "coco_pretrained"): detector.get("allowed_classes", []),
        },
        "tracking": {
            "tracker": tracking.get("tracker", "bytetrack.yaml"),
            "active_track_timeout_frames": tracking.get("active_track_timeout_frames", 10),
            "min_track_age_frames": tracking.get("min_track_age_frames", 3),
        },
        "smoothing": {
            "method": "hybrid",
            "history_window": 5,
            "min_consecutive_for_change": 1,
        },
        "lane_assignment": {
            "boundary_mode": "inside_or_on_edge",
            "unknown_policy": "keep_last_stable",
            "unknown_timeout_frames": 15,
        },
        "counting": {
            "min_cross_distance_px": counting.get("min_cross_distance_px", 2.0),
            "count_unstable_lane": True,
        },
        "roi_padding": detector.get("roi_padding", 80),
        "lanes": lanes,
    }


def _native_imgsz(width: int, height: int, stride: int = 32) -> int:
    """YOLO imgsz = longest edge rounded up to stride (no intentional downscale)."""
    longest = max(int(width), int(height), stride)
    return ((longest + stride - 1) // stride) * stride


def _build_live_runtime(
    camera_id: str,
    cfg: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], CropROI | None, int, int]:
    """Build the in-memory lane/ROI runtime from the current YAML files."""
    pipeline_cfg = _cfg_to_pipeline_dict(
        cfg,
        camera_cfg_path=Path(f"configs/cameras/{camera_id}.yaml"),
    )
    lanes = pipeline_cfg["lanes"]
    roi: CropROI | None = None
    roi_crop_enabled = pipeline_cfg.get("detector", {}).get("roi_crop", True)
    zone_polygons = _load_zones(cfg) if roi_crop_enabled else []

    # Keep original dimensions before transform_config changes frame_size to
    # crop dimensions. Zones are valid even when no lanes are configured.
    original_frame_w = pipeline_cfg["frame_size"]["width"]
    original_frame_h = pipeline_cfg["frame_size"]["height"]
    if roi_crop_enabled and (lanes or zone_polygons):
        try:
            roi_padding = int(pipeline_cfg.get("roi_padding", 80))
            roi = CropROI(
                lanes,
                pipeline_cfg["frame_size"],
                padding=roi_padding,
                zone_polygons=zone_polygons,
            )
            pipeline_cfg = roi.transform_config(pipeline_cfg)
            pipeline_cfg.setdefault("detector", {})
            # Native crop resolution for max detection quality (no downscale).
            pipeline_cfg["detector"]["imgsz"] = roi.suggested_imgsz()
            logger.info(
                "ROI crop: %s (area ratio: %.2f%%), imgsz=%d conf=%.2f max_det=%s every_n=%s",
                roi,
                roi.area_ratio * 100,
                pipeline_cfg["detector"]["imgsz"],
                float(pipeline_cfg["detector"].get("conf", 0.25)),
                pipeline_cfg["detector"].get("max_detections", 500),
                pipeline_cfg["detector"].get("detect_every_n_frames", 1),
            )
        except Exception:
            logger.warning("Failed to init CropROI — full frame", exc_info=True)
            roi = None

    if roi is None:
        pipeline_cfg.setdefault("detector", {})
        pipeline_cfg["detector"]["imgsz"] = _native_imgsz(
            original_frame_w, original_frame_h
        )

    return pipeline_cfg, lanes, roi, original_frame_w, original_frame_h


def _request_stream_reload(camera_id: str) -> bool:
    """Request a config-only hot reload without dropping the video reader."""
    with _lock:
        stream = _streams.get(camera_id)
        if stream is None:
            return False
        meta = stream[2]
        meta["reload_requested"] = True
        meta["reload_requested_at"] = time.monotonic()
        return True


def _record_source_failure(
    camera_id: str,
    source_type: str,
    source: str,
    exc: BaseException,
    *,
    status: str = "reconnecting",
    stream_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish one structured source failure to metrics, alerts and logs."""
    diagnostic = diagnose_stream_error(
        exc,
        source_type=source_type,
        source=source,
    )
    from tf_common.alert_service import alert_service
    from tf_common.monitoring.live_metrics import record_stream_state

    record_stream_state(
        camera_id,
        status,
        diagnostic["message"],
        error_code=diagnostic["code"],
        error_details=diagnostic,
    )
    if stream_meta is not None:
        stream_meta["last_error_code"] = diagnostic["code"]
        stream_meta["last_error_details"] = diagnostic
        stream_meta["source_failure_seen"] = True
    alert_service.emit(
        diagnostic["severity"],
        diagnostic["title"],
        diagnostic["message"],
        camera_id=camera_id,
        alert_type="stream_source_error",
        details=diagnostic,
    )
    logger.warning(
        "Camera %s source failure code=%s type=%s: %s",
        camera_id,
        diagnostic["code"],
        source_type,
        type(exc).__name__,
    )
    return diagnostic


def _start_pipeline(
    camera_id: str,
    cfg: dict[str, Any],
) -> tuple[DetectionCore, Queue, dict[str, Any], Any, Any, threading.Event]:
    """Start a live detection pipeline with DB persistence and Redis publishing.

    Returns
    -------
    tuple of (DetectionCore, annotated_queue, StorageWorker, db_session, stop_event)
    """
    pipeline_cfg, lanes, roi, _original_frame_w, _original_frame_h = _build_live_runtime(
        camera_id,
        cfg,
    )
    source = cfg.get("source") or cfg.get("server", {}).get("source", "")
    source_type = cfg.get("source_type") or cfg.get("input", {}).get("source_type", "video")

    core = DetectionCore(pipeline_cfg)
    core.start()

    # ── DB persistence ──────────────────────────────────────────────────────
    import os

    from tf_api.storage_adapters import make_server_adapters
    from tf_common.pubsub import RedisPublisher
    from tf_db.session import SessionLocal
    from tf_worker.storage.storage_worker import StorageWorker
    redis_enabled = os.getenv("REDIS_HOST") is not None
    publisher = RedisPublisher(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
    ) if redis_enabled else None

    from tf_common.live_bus import LiveEventBus

    storage_root = Path(os.getenv("STORAGE_ROOT", "data/storage")) / camera_id
    storage_root.mkdir(parents=True, exist_ok=True)
    storage_worker = StorageWorker(
        storage_root=storage_root,
        adapter_factory=lambda: make_server_adapters(SessionLocal()),
        publisher=publisher,
    )

    # Latest-frame only: size 1 keeps latency low and avoids backlog stutter.
    annotated_queue: Queue = Queue(maxsize=1)
    preview_defaults = get_preview_defaults()
    # Full source resolution is preserved; quality is tuned for encode speed so
    # Output FPS stays close to the preview target on laptop GPUs.
    jpeg_quality = int(preview_defaults.get("jpeg_quality", 72))
    jpeg_quality = max(55, min(90, jpeg_quality))
    preview_target_fps = float(preview_defaults.get("target_fps", 20) or 20)
    preview_target_fps = max(8.0, min(30.0, preview_target_fps))
    stream_meta: dict[str, Any] = {
        "source_fps": float(cfg.get("fps", 25.0) or 25.0),
        "preview_fps_target": preview_target_fps,
        "preview_jpeg_quality": jpeg_quality,
        "preview_width": int(_original_frame_w),
        "preview_height": int(_original_frame_h),
        "preserve_source_resolution": bool(
            preview_defaults.get("preserve_source_resolution", True)
        ),
        "connections": 0,
        "last_access": time.monotonic(),
    }
    stop_event = threading.Event()
    from tf_common.monitoring.live_metrics import record_stream_state
    record_stream_state(camera_id, "starting")

    # ── Full-resolution latest-frame preview encoder ─────────────────────
    # Capture never blocks on JPEG. Encoder takes ownership of the newest
    # frame reference (single copy on publish) and encodes as fast as possible
    # up to preview_target_fps — no forced sleep that starves Output FPS.
    _preview_lock = threading.Lock()
    _preview_frame: np.ndarray | None = None
    _preview_seq = 0

    def _publish_preview_frame(annotated: np.ndarray) -> None:
        """Store a full-res copy of the newest annotated frame for encode."""
        nonlocal _preview_frame, _preview_seq
        # New array each time so the encode thread can hold the previous ref
        # without a second copy, and without racing capture in-place draws.
        frame_copy = annotated.copy()
        with _preview_lock:
            _preview_frame = frame_copy
            _preview_seq += 1

    def _encode_worker():
        """Encode newest full-res frame; drop intermediate frames under load."""
        nonlocal _preview_frame, _preview_seq
        last_seq = -1
        min_interval = 1.0 / preview_target_fps
        last_emit = 0.0
        while not stop_event.is_set():
            frame_ref: np.ndarray | None = None
            with _preview_lock:
                if _preview_frame is not None and _preview_seq != last_seq:
                    last_seq = _preview_seq
                    frame_ref = _preview_frame
            if frame_ref is None:
                time.sleep(0.005)
                continue

            # Cap emit rate without sleeping the full interval after a slow encode.
            now = time.monotonic()
            wait = min_interval - (now - last_emit)
            if wait > 0:
                time.sleep(wait)

            try:
                ok, jpeg = cv2.imencode(
                    ".jpg",
                    frame_ref,
                    [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality],
                )
                if not ok:
                    continue
                jpeg_bytes = jpeg.tobytes()
                try:
                    annotated_queue.put_nowait(jpeg_bytes)
                except Full:
                    with suppress(Empty):
                        annotated_queue.get_nowait()
                    with suppress(Full):
                        annotated_queue.put_nowait(jpeg_bytes)
                last_emit = time.monotonic()
            except (ValueError, cv2.error):
                logger.debug("Preview encode failed for %s", camera_id, exc_info=True)

    encode_thread = threading.Thread(
        target=_encode_worker,
        daemon=True,
        name=f"enc-{camera_id}",
    )
    encode_thread.start()

    # MotionDetector is auto-disabled for youtube_live/rtsp (always True).
    # Keep for file sources only — live traffic must not skip YOLO frames.
    _motion_detector = (
        _MotionDetector(threshold=0.02, source_type=source_type)
        if _MotionDetector is not None
        else None
    )
    _last_det: dict[str, Any] | None = None
    _last_published_occupancy: dict[str, int] | None = None
    # Unique track IDs seen during this live pipeline session, used to build
    # Vehicle Types (Session) from actual detector class labels (not DB
    # line-crossing aggregates, which stay empty without counting lines).
    _session_class_counts: dict[str, int] = {}
    _session_seen_track_ids: set[int] = set()
    _last_published_vehicle_types: dict[str, int] | None = None
    stream_meta["vehicle_types"] = {}
    stream_meta["occupancy"] = {}

    def _capture_loop():
        nonlocal core, lanes, roi, _original_frame_w, _original_frame_h
        nonlocal _last_det, _last_published_occupancy, _last_published_vehicle_types
        attempt = 0
        while not stop_event.is_set():
            try:
                from tf_worker.io.video_io import get_video_reader

                reader = get_video_reader(
                    source,
                    {
                        "input": {
                            "source_type": source_type,
                            "fps": cfg.get("fps", 25),
                            "target_fps": cfg.get("fps", 25),
                        }
                    },
                )
                source_fps = float(getattr(reader, "get_fps", 0.0) or cfg.get("fps", 25) or 25.0)
                stream_meta["source_fps"] = round(source_fps, 1)
                attempt = 0  # reset after successful connect
                last_diagnostic = stream_meta.get("last_error_details")
                if last_diagnostic:
                    record_stream_state(
                        camera_id,
                        "connecting",
                        last_diagnostic["message"],
                        error_code=last_diagnostic["code"],
                        error_details=last_diagnostic,
                    )
                else:
                    record_stream_state(camera_id, "connecting")
            except (OSError, ValueError, RuntimeError) as exc:
                diagnostic = _record_source_failure(
                    camera_id,
                    source_type,
                    source,
                    exc,
                    stream_meta=stream_meta,
                )
                attempt += 1
                if attempt > _MAX_RECONNECT_ATTEMPTS:
                    logger.error("Camera %s: max reconnection attempts reached", camera_id)
                    record_stream_state(
                        camera_id,
                        "error",
                        "Camera source unavailable after reconnection attempts",
                        error_code=diagnostic["code"],
                        error_details=diagnostic,
                    )
                    # Emit camera_offline alert
                    from tf_common.alert_service import alert_service as _as
                    _as.emit("critical", f"Camera Offline — {camera_id}",
                             f"Connection lost after {_MAX_RECONNECT_ATTEMPTS} attempts. Check source or network.",
                             camera_id=camera_id, alert_type="camera_offline")
                    break
                backoff = min(
                    _RECONNECT_BACKOFF_BASE * (2 ** (attempt - 1)),
                    _RECONNECT_BACKOFF_MAX,
                )
                logger.warning(
                    "Camera %s: reconnection attempt %d/%d in %.1fs",
                    camera_id, attempt, _MAX_RECONNECT_ATTEMPTS, backoff,
                )
                record_stream_state(
                    camera_id,
                    "reconnecting",
                    f"{diagnostic['message']} Connection attempt {attempt}/{_MAX_RECONNECT_ATTEMPTS}.",
                    error_code=diagnostic["code"],
                    error_details=diagnostic,
                )
                time.sleep(backoff)
                continue

            frame_idx = 0
            read_failures = 0
            try:
                while not stop_event.is_set():
                    if stream_meta.pop("reload_requested", False):
                        try:
                            new_cfg = _load_camera_config(camera_id)
                            if new_cfg is None:
                                raise ValueError(f"Camera config disappeared: {camera_id}")
                            (
                                new_pipeline_cfg,
                                new_lanes,
                                new_roi,
                                new_frame_w,
                                new_frame_h,
                            ) = _build_live_runtime(camera_id, new_cfg)
                            new_core = DetectionCore(new_pipeline_cfg)
                            new_core.start()

                            # Swap only inference/config state. The existing
                            # reader keeps delivering frames, so a Lane/Zone
                            # save cannot make the browser lose its video.
                            core = new_core
                            lanes = new_lanes
                            roi = new_roi
                            _original_frame_w = new_frame_w
                            _original_frame_h = new_frame_h
                            _last_det = None
                            _last_published_occupancy = None
                            record_stream_state(camera_id, "connecting")
                            logger.info("Hot-reloaded lanes/zones for %s", camera_id)
                        except Exception:
                            logger.exception("Failed to hot-reload lanes/zones for %s", camera_id)
                            record_stream_state(camera_id, "error", "Lane/zone hot reload failed")

                    success, frame = reader.read()
                    if not success or frame is None:
                        read_failures += 1
                        if read_failures > 30:  # ~3 seconds of read failures
                            logger.warning("Camera %s: too many read failures, reconnecting", camera_id)
                            _record_source_failure(
                                camera_id,
                                source_type,
                                source,
                                RuntimeError("camera reader returned no frame"),
                                stream_meta=stream_meta,
                            )
                            break
                        time.sleep(0.1)
                        continue
                    read_failures = 0

                    always_on = bool(stream_meta.get("always_on"))
                    viewers = int(stream_meta.get("connections", 0) or 0)
                    last_access = float(stream_meta.get("last_access", 0.0) or 0.0)
                    idle_for = time.monotonic() - last_access
                    # always_on pipelines keep detecting so Dashboard/Events get data
                    # without a browser watching MJPEG. Viewer-only pipelines may
                    # idle-skip when nobody is connected.
                    if always_on:
                        stream_meta["last_access"] = time.monotonic()
                    elif viewers <= 0 and idle_for > 2.0:
                        _update_snapshot(camera_id, frame)
                        time.sleep(0.03)
                        frame_idx += 1
                        continue

                    _update_snapshot(camera_id, frame)

                    # Serve MJPEG at ORIGINAL resolution.  The pipeline runs only on
                    # the ROI crop (lane polygon union + padding), so the model
                    # never sees the full frame — just the relevant area.
                    orig_h, orig_w = frame.shape[:2]
                    # Use original frame size (pre-crop), NOT the crop size
                    # from pipeline_cfg (which transform_config overwrote).
                    tgt_w = _original_frame_w
                    tgt_h = _original_frame_h

                    # 1. Resize to a consistent size for the detection pipeline
                    t_resize = time.perf_counter()
                    if orig_w != tgt_w or orig_h != tgt_h:
                        pipeline_frame = cv2.resize(frame, (tgt_w, tgt_h))
                    else:
                        pipeline_frame = frame
                    _log_timing(camera_id, "resize", (time.perf_counter() - t_resize) * 1000)

                    # 2. ROI crop on pipeline-sized copy
                    frame_roi = roi.crop(pipeline_frame) if roi is not None else pipeline_frame

                    # Motion-gated detection — skips YOLO entirely when nothing moves.
                    # The MotionDetector uses a cheap 160 px frame-differencing check
                    # (< 0.5 ms per frame) and when no motion is detected we reuse the
                    # last detection result.  On busy scenes detection runs every frame.
                    t_detect_start = time.perf_counter()
                    det = None
                    if _motion_detector is not None and _motion_detector.has_motion(frame_roi) is False:
                        det = deepcopy(_last_det) if _last_det is not None else None
                        if det is not None:
                            det["frame_idx"] = frame_idx
                            det["frame_timestamp"] = datetime.now(timezone.utc)
                    if det is None:  # first frame, motion detected, or no motion detector
                        det = core.process_frame(frame_roi, frame_idx=frame_idx)
                        _last_det = deepcopy(det)
                    detect_ms = (time.perf_counter() - t_detect_start) * 1000
                    _log_timing(camera_id, "detect", detect_ms)

                    # 3. Keep the core/tracker result in crop-space.  The
                    # tracker and LaneStateManager retain references to these
                    # bbox lists; mutating them in-place to original-frame
                    # coordinates corrupts ByteTrack's cache and causes the
                    # ROI offset to be added again on later frames.
                    #
                    # Make a display/storage copy instead and restore only
                    # that copy to original-frame coordinates.
                    display_det = deepcopy(det)
                    _restore_original_bboxes(
                        display_det,
                        roi,
                        _original_frame_w,
                        _original_frame_h,
                    )

                    # 4. Record metrics
                    timing = dict(det.get("timing_ms", {}))
                    timing.update(_perf_timings.pop(camera_id, {}))
                    from tf_common.monitoring.live_metrics import record_frame
                    record_frame(camera_id, timing, source_fps=source_fps)
                    if stream_meta.pop("source_failure_seen", False):
                        from tf_common.alert_service import alert_service as _as
                        _as.resolve("stream_source_error", camera_id)
                        _as.resolve("camera_offline", camera_id)
                        stream_meta.pop("last_error_code", None)
                        stream_meta.pop("last_error_details", None)

                    # ── Extract crossing crops from clean frame BEFORE annotation ──
                    # This avoids annotating in-place then reading back annotated pixels
                    # for crop extraction.  Moving it earlier also parallelises work:
                    # the MJPEG queue push and StorageWorker enqueue can overlap.
                    crossings = display_det.get("crossings", [])
                    bbox_by_track_id = {
                        track.get("track_id"): track.get("bbox")
                        for track in display_det.get("frame_tracks", [])
                        if track.get("track_id") is not None and track.get("bbox")
                    }
                    crop_data: list[tuple[str, int, str, str, float, list[float] | None, bytes | None]] = []
                    for cx in crossings:
                        track_id = cx.get("track_id")
                        bbox = bbox_by_track_id.get(track_id)
                        if bbox is None:
                            state = core.state_manager.track_states.get(track_id)
                            state_bbox = state.bbox if state is not None else None
                            if state_bbox is not None and len(state_bbox) == 4:
                                bbox = list(state_bbox)
                                if roi is not None:
                                    bbox = roi.to_original(bbox, _original_frame_w, _original_frame_h)
                        crop_bytes: bytes | None = None
                        if bbox is not None:
                            try:
                                x1, y1, x2, y2 = [int(v) for v in bbox]
                                h, w = pipeline_frame.shape[:2]
                                x1, y1 = max(0, x1), max(0, y1)
                                x2, y2 = min(w, x2), min(h, y2)
                                if x2 > x1 and y2 > y1:
                                    crop = pipeline_frame[y1:y2, x1:x2]
                                    _, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 60])
                                    crop_bytes = encoded.tobytes()
                            except Exception:
                                crop_bytes = None
                        crop_data.append((
                            cx.get("lane_id", "unknown"),
                            cx.get("track_id", -1),
                            cx.get("class_name", "unknown"),
                            cx.get("direction", ""),
                            cx.get("confidence", 0.0),
                            bbox,
                            crop_bytes,
                        ))

                    # 5. Annotate + encode only when someone is watching MJPEG.
                    # always_on detection still runs above so fleet data stays fresh
                    # without paying full annotate/JPEG cost per headless camera.
                    if viewers > 0:
                        t_annotate = time.perf_counter()
                        annotated = _annotate(pipeline_frame, display_det, lanes)
                        _log_timing(
                            camera_id, "annotate",
                            (time.perf_counter() - t_annotate) * 1000,
                        )
                        t_submit = time.perf_counter()
                        _publish_preview_frame(annotated)
                        _log_timing(
                            camera_id, "preview_publish",
                            (time.perf_counter() - t_submit) * 1000,
                        )

                    # ── Persist crossing events to DB ──────────────────
                    for (lane_id, track_id, class_name, direction,
                         confidence, bbox, crop_bytes) in crop_data:
                        storage_worker.enqueue(
                            camera_id=camera_id,
                            job_id=f"live-{camera_id}",
                            lane_id=lane_id,
                            track_id=track_id,
                            vehicle_type=class_name,
                            direction=direction,
                            confidence=confidence,
                            frame_id=frame_idx,
                            timestamp=datetime.now(timezone.utc),
                            frame=None,  # never store full frame in queue — OOM risk
                            bbox=bbox,
                            crop_bytes=crop_bytes,
                        )

                    # ── Session vehicle types from raw track detections ──
                    # Count each track_id once using the class_name emitted by
                    # the detector/tracker. This powers Live → Vehicle Types
                    # even when no counting line is configured (so DB crossing
                    # events / count_event never fire).
                    for track in det.get("frame_tracks") or det.get("tracks") or []:
                        tid = track.get("track_id")
                        cls = str(track.get("class_name") or "").strip().lower()
                        if tid is None or not cls or cls == "unknown":
                            continue
                        try:
                            tid_int = int(tid)
                        except (TypeError, ValueError):
                            continue
                        if tid_int in _session_seen_track_ids:
                            continue
                        _session_seen_track_ids.add(tid_int)
                        _session_class_counts[cls] = (
                            _session_class_counts.get(cls, 0) + 1
                        )

                    vehicle_types = dict(_session_class_counts)
                    stream_meta["vehicle_types"] = vehicle_types

                    # ── Publish live occupancy snapshot ────────────────
                    occupancy = det.get("occupancy", {}) or {}
                    # Always mirror the latest live occupancy into stream_meta so
                    # /live/{id}/metrics can hydrate the SPA per-camera on load
                    # (DB occupancy/latest is line-crossing based and often empty).
                    stream_meta["occupancy"] = dict(occupancy)
                    # Publish an initial/changed snapshot even when the camera
                    # is empty. The old truthy-only check left the UI stale at
                    # zero and the Redis payload lacked the frontend event
                    # envelope (type/camera_id/data).
                    if (
                        _last_published_occupancy != occupancy
                        or _last_published_vehicle_types != vehicle_types
                        or frame_idx % 10 == 0
                    ):
                        live_message = {
                            "type": "occupancy_update",
                            "camera_id": camera_id,
                            "data": {
                                "occupancy": occupancy,
                                "vehicle_types": vehicle_types,
                                "frame_idx": frame_idx,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        }
                        LiveEventBus.publish(camera_id, live_message)
                        if publisher is not None:
                            try:
                                publisher.publish_live_state(camera_id, live_message)
                            except Exception:
                                logger.debug("Failed to publish live state for %s", camera_id, exc_info=True)
                        _last_published_occupancy = dict(occupancy)
                        _last_published_vehicle_types = dict(vehicle_types)

                    # Publish crossing and lane-change events through Redis as
                    # well as the in-process bus. The latter only works when
                    # API and capture loop share a process, which is not true
                    # for the Docker worker/API deployment.
                    for cx in crossings:
                        event_message = {
                            "type": "count_event",
                            "camera_id": camera_id,
                            "data": cx,
                        }
                        LiveEventBus.publish(camera_id, event_message)
                        if publisher is not None:
                            publisher.publish_live_state(camera_id, event_message)
                    for event in det.get("events", []) or []:
                        lane_message = {
                            "type": "lane_change_event",
                            "camera_id": camera_id,
                            "data": {
                                **event,
                                "previous_lane_id": event.get(
                                    "previous_lane_id", event.get("previous_stable_lane")
                                ),
                                "current_lane_id": event.get(
                                    "current_lane_id", event.get("current_stable_lane")
                                ),
                                "frame_id": event.get("frame_id", event.get("frame", frame_idx)),
                            },
                        }
                        LiveEventBus.publish(camera_id, lane_message)
                        if publisher is not None:
                            publisher.publish_live_state(camera_id, lane_message)
                    frame_idx += 1

            finally:
                reader.release()

    capture_thread = threading.Thread(
        target=_capture_loop,
        daemon=True,
        name=f"cam-{camera_id}",
    )
    capture_thread.start()
    stream_meta["threads"] = [capture_thread, encode_thread]
    return core, annotated_queue, stream_meta, storage_worker, None, stop_event


def _annotate(frame: np.ndarray, det: dict[str, Any], lanes: list[dict]) -> np.ndarray:
    canvas = frame  # annotate in-place — saves ~6 MB copy per frame
    lane_colors = build_lane_color_map([lane["id"] for lane in lanes])

    crossings = det.get("crossings", [])
    for lane in lanes:
        cl = lane.get("counting_line")
        if cl:
            sx, sy = cl["start"]
            ex, ey = cl["end"]
            line_id = f"{lane['id']}_count"
            flashed = any(c.get("line_id") == line_id for c in crossings)
            color = (255, 255, 255) if flashed else (0, 220, 255)
            cv2.line(canvas, (int(sx), int(sy)), (int(ex), int(ey)), color, 3)
            cv2.putText(canvas, line_id, (int(sx) + 5, int(sy) - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
            cv2.putText(canvas, line_id, (int(sx) + 5, int(sy) - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    for lane_data in lanes:
        lane_color = color_for_lane(lane_data["id"], lane_colors)
        pts = np.array(lane_data["points"], dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(canvas, [pts], isClosed=True, color=lane_color, thickness=2)
        first_pt = lane_data["points"][0]
        cv2.putText(canvas, lane_data["id"], (int(first_pt[0]) + 5, int(first_pt[1]) - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.putText(canvas, lane_data["id"], (int(first_pt[0]) + 5, int(first_pt[1]) - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, lane_color, 1)

    ftracks = det.get("frame_tracks", [])
    for t in ftracks:
        bbox = t.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = [int(v) for v in bbox]
        # Same lane → same box color (stable preferred, raw as fallback).
        box_color = color_for_track(
            lane_colors,
            stable_lane=t.get("stable_lane"),
            raw_lane=t.get("raw_lane"),
        )
        stable = t.get("stable_lane") or t.get("raw_lane") or "none"
        cv2.rectangle(canvas, (x1, y1), (x2, y2), box_color, 2)
        cx = (x1 + x2) // 2
        cv2.circle(canvas, (cx, y2), 5, box_color, -1)
        label = f"#{t['track_id']} {t['class_name']} ({stable})"
        # Dark outline + lane-colored label keeps text readable on busy video.
        cv2.putText(canvas, label, (x1 + 3, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 2)
        cv2.putText(canvas, label, (x1 + 3, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, box_color, 1)

    occupancy = det.get("occupancy", {})
    if occupancy:
        panel_w, panel_h = 200, 40 + len(occupancy) * 25
        # Blend only the panel region (small 200xN overlay) instead of full-frame copy
        panel_overlay = np.full((panel_h, panel_w, 3), (20, 20, 20), dtype=np.uint8)
        panel_roi = canvas[15:15 + panel_h, 15:15 + panel_w]
        cv2.addWeighted(panel_overlay, 0.75, panel_roi, 0.25, 0, panel_roi)
        cv2.rectangle(canvas, (15, 15), (15 + panel_w, 15 + panel_h), (80, 80, 80), 1)
        cv2.putText(canvas, "LANE OCCUPANCY", (25, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
        cv2.line(canvas, (25, 42), (15 + panel_w - 10, 42), (80, 80, 80), 1)
        y = 62
        for lane_id in sorted(occupancy.keys()):
            lane_color = color_for_lane(lane_id, lane_colors)
            cv2.putText(canvas, f"{lane_id}:", (25, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, lane_color, 1)
            cv2.putText(canvas, str(occupancy[lane_id]), (15 + panel_w - 40, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, lane_color, 2)
            y += 25

    for cx in crossings[:1]:
        cv2.putText(canvas, f"{cx['class_name']} #{cx['track_id']} {cx['direction']}",
                    (20, canvas.shape[0] - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)

    return canvas


def _mjpeg_generator(
    camera_id: str,
    queue: Queue,
    fps: float = 25.0,
    stop_event: threading.Event | None = None,
):
    """Stream fresh MJPEG frames until the pipeline is stopped or disconnected.

    Pacing is owned by the encode worker (stable preview FPS at full source
    resolution). This generator emits every queued JPEG without dropping —
    extra rate-limiting here was a major source of output FPS jitter.
    """
    from tf_common.monitoring.live_metrics import record_output_frame

    # Poll slightly faster than preview target so the queue never backs up.
    poll = min(0.5, 1.0 / max(fps * 2.0, 2.0))
    last_fresh = 0.0
    last_stall_emit = 0.0
    # Pre-compute a "NO SIGNAL" placeholder (encoded once, reused on stalls).
    _no_signal: bytes | None = None

    try:
        while stop_event is None or not stop_event.is_set():
            try:
                jpeg_bytes = queue.get(timeout=poll)
            except Empty:
                jpeg_bytes = None

            now = time.monotonic()
            if jpeg_bytes is None:
                if last_fresh and (now - last_fresh) >= 2.0 and (now - last_stall_emit) >= 1.0:
                    if _no_signal is None:
                        blank = np.zeros((540, 960, 3), dtype=np.uint8)
                        cv2.putText(blank, "NO SIGNAL",
                                    (960 // 4, 540 // 2),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                                    (100, 100, 100), 3)
                        _, enc = cv2.imencode(".jpg", blank,
                                              [cv2.IMWRITE_JPEG_QUALITY, 60])
                        _no_signal = enc.tobytes()
                    last_stall_emit = now
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + _no_signal + b"\r\n"
                    )
                continue

            last_fresh = now
            record_output_frame(camera_id)
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpeg_bytes + b"\r\n"
            )
    finally:
        with _lock:
            if camera_id in _streams:
                meta = _streams[camera_id][2]
                meta["connections"] = max(0, meta.get("connections", 0) - 1)
                meta["last_access"] = time.monotonic()


def _cleanup_stream(camera_id: str) -> bool:
    """Forcefully stop and remove a live pipeline stream. Returns True if existed."""
    with _lock:
        if camera_id not in _streams:
            return False
        _core, _q, _meta, sw, sess, stop_ev = _streams.pop(camera_id)
        _last_snapshots.pop(camera_id, None)
    stop_ev.set()
    for thread in _meta.get("threads", []):
        if thread is not threading.current_thread():
            thread.join(timeout=5.0)
    if sw is not None:
        try:
            sw.stop(timeout=3.0)
        except Exception:
            logger.warning("Failed to stop StorageWorker for %s", camera_id, exc_info=True)
    if sess is not None:
        try:
            sess.close()
        except Exception:
            logger.debug("Failed to close stream session for %s", camera_id, exc_info=True)
    from tf_common.monitoring.live_metrics import record_stream_stopped
    record_stream_stopped(camera_id)
    logger.info("Cleaned up stream: %s", camera_id)
    return True


@router.post("/live/{camera_id}/reload")
async def reload_live_pipeline(
    camera_id: str,
    model_id: str | None = None,
    _user: dict = Depends(require_operator),
):
    """Reload the live pipeline for a camera — picks up new lane config and model.

    If *model_id* is provided the camera config YAML is updated so the
    new pipeline uses the requested weights.
    """
    if model_id:
        # Update camera config YAML with the new model_id
        cfg = _load_camera_config(camera_id)
        if cfg is not None:
            cfg.setdefault("model", {})["model_id"] = model_id
            cam_yaml = Path(f"configs/cameras/{camera_id}.yaml")
            with open(cam_yaml, encoding="utf-8") as f:  # noqa: ASYNC230 - small local YAML file
                raw = yaml.safe_load(f) or {}
            raw.setdefault("model", {})["model_id"] = model_id
            with open(cam_yaml, "w", encoding="utf-8") as f:  # noqa: ASYNC230 - small local YAML file
                yaml.safe_dump(raw, f, sort_keys=False)

    _cleanup_stream(camera_id)
    return {"status": "reloaded", "camera_id": camera_id, "model_id": model_id}


@router.get("/live/status")
async def live_fleet_status(_user: dict = Depends(get_current_user)):
    """Fleet overview of always-on / live detection pipelines.

    Used by Dashboard and ops to confirm cameras are feeding realtime data
    without opening Live Monitoring.
    """
    from tf_common.monitoring.live_metrics import get_camera_metrics

    configured = list_configured_camera_ids()
    cameras: list[dict[str, Any]] = []
    with _lock:
        for camera_id in configured:
            stream = _streams.get(camera_id)
            meta = stream[2] if stream is not None else {}
            metrics = get_camera_metrics(camera_id)
            cameras.append({
                "camera_id": camera_id,
                "running": stream is not None,
                "always_on": bool(meta.get("always_on")),
                "viewers": int(meta.get("connections") or 0),
                "status": metrics.get("status"),
                "process_fps": metrics.get("process_fps") or metrics.get("fps") or 0,
                "output_fps": metrics.get("output_fps") or 0,
                "vehicle_types": dict(meta.get("vehicle_types") or {}),
                "occupancy": dict(meta.get("occupancy") or {}),
                "error": metrics.get("error"),
            })
    running = sum(1 for c in cameras if c["running"])
    return {
        "auto_start_enabled": _env_auto_start_live(),
        "supervisor_interval_sec": _SUPERVISOR_INTERVAL_SEC,
        "configured": len(configured),
        "running": running,
        "always_on": sum(1 for c in cameras if c["always_on"]),
        "cameras": cameras,
    }


@router.get("/live/{camera_id}/metrics")
async def live_camera_metrics(camera_id: str, _user: dict = Depends(get_current_user)):
    """Return real-time input/process/output FPS, latency, and GPU metrics."""
    from tf_common.monitoring.live_metrics import get_camera_metrics
    result = get_camera_metrics(camera_id)
    # Attach per-camera live pipeline state (session vehicle types + occupancy)
    # so the SPA can hydrate panels on camera switch without relying on DB
    # line-crossing aggregates.
    with _lock:
        stream = _streams.get(camera_id)
        if stream is not None:
            meta = stream[2]
            result["vehicle_types"] = dict(meta.get("vehicle_types") or {})
            result["occupancy"] = dict(meta.get("occupancy") or {})
            result["always_on"] = bool(meta.get("always_on"))
            result["viewers"] = int(meta.get("connections") or 0)
            result["pipeline_running"] = True
            result["preview"] = {
                "width": meta.get("preview_width"),
                "height": meta.get("preview_height"),
                "target_fps": meta.get("preview_fps_target"),
                "jpeg_quality": meta.get("preview_jpeg_quality"),
                "preserve_source_resolution": meta.get(
                    "preserve_source_resolution", True
                ),
            }
        else:
            result.setdefault("vehicle_types", {})
            result.setdefault("occupancy", {})
            result["always_on"] = False
            result["viewers"] = 0
            result["pipeline_running"] = False
    result["auto_start_enabled"] = _env_auto_start_live()
    return result


@router.post("/live/{camera_id}/verify-source")
async def verify_live_source(
    camera_id: str,
    _user: dict = Depends(require_operator),
):
    """Verify the configured source without starting a second live pipeline.

    YouTube extraction is deliberately executed in a worker thread because
    yt-dlp performs blocking network and retry operations.  The endpoint does
    not return the resolved HLS URL; it only reports whether extraction works.
    """
    validate_identifier(camera_id, name="camera_id")
    cfg = _load_camera_config(camera_id)
    if cfg is None:
        raise HTTPException(404, f"Camera not found: {camera_id}")

    source = str(cfg.get("source") or cfg.get("server", {}).get("source", ""))
    source_type = str(
        cfg.get("source_type")
        or cfg.get("input", {}).get("source_type", "video")
    )
    if source_type not in {"youtube", "youtube_live"}:
        from tf_common.monitoring.live_metrics import get_camera_metrics

        current = get_camera_metrics(camera_id)
        is_ok = current.get("status") in {"active", "connecting"}
        return {
            "ok": is_ok,
            "camera_id": camera_id,
            "diagnostic": {
                "code": "SOURCE_STATUS_CHECKED",
                "severity": "info" if is_ok else "warning",
                "title": "Source status checked",
                "message": (
                    "Nguồn hiện đang có kết nối."
                    if is_ok
                    else "Chưa có frame hoạt động để xác minh nguồn này."
                ),
                "cause": current.get("error"),
                "fix_steps": [],
                "verify_steps": [
                    "Theo dõi Process và Output FPS; giá trị phải lớn hơn 0 khi có frame.",
                ],
                "source_type": source_type,
                "retryable": True,
            },
        }

    try:
        from tf_common.yt_utils import resolve_stream_info

        await run_in_threadpool(
            lambda: resolve_stream_info(
                source,
                fmt=cfg.get("input", {}).get("yt_format", "best[height<=720]"),
                retries=1,
                use_cache=False,
                allow_stale_cache=False,
            )
        )
    except Exception as exc:
        diagnostic = diagnose_stream_error(
            exc,
            source_type=source_type,
            source=source,
        )
        from tf_common.alert_service import alert_service
        from tf_common.monitoring.live_metrics import record_stream_diagnostic

        record_stream_diagnostic(camera_id, diagnostic)
        alert_service.emit(
            diagnostic["severity"],
            diagnostic["title"],
            diagnostic["message"],
            camera_id=camera_id,
            alert_type="stream_source_error",
            details=diagnostic,
        )
        return JSONResponse(
            {
                "ok": False,
                "camera_id": camera_id,
                "diagnostic": diagnostic,
            },
            status_code=503,
        )

    diagnostic = {
        "code": "YOUTUBE_SOURCE_VERIFIED",
        "severity": "info",
        "title": "YouTube source verified",
        "message": "yt-dlp đã lấy được playable stream metadata từ nguồn YouTube.",
        "cause": None,
        "fix_steps": [],
        "verify_steps": [
            "Nếu video vẫn chưa hiện, bấm Retry stream để mở lại MJPEG pipeline.",
            "Xác nhận Process và Output FPS lớn hơn 0.",
        ],
        "source_type": source_type,
        "retryable": True,
    }
    from tf_common.monitoring.live_metrics import record_stream_diagnostic
    from tf_common.alert_service import alert_service

    record_stream_diagnostic(camera_id, diagnostic)
    alert_service.resolve("stream_source_error", camera_id)
    return {"ok": True, "camera_id": camera_id, "diagnostic": diagnostic}


def _authorize_live_request(
    cred: HTTPAuthorizationCredentials | None,
    stream_token: str | None,
    camera_id: str,
) -> dict[str, Any]:
    if cred is not None:
        return decode_access_token(cred.credentials)
    if stream_token:
        with _lock:
            ticket = _stream_tickets.pop(stream_token, None)
        if ticket is not None:
            ticket_camera, expires_at = ticket
            if ticket_camera == camera_id and time.monotonic() < expires_at:
                return {"sub": "stream-ticket", "role": "viewer"}
    raise HTTPException(401, "Missing authorization header")


@router.get("/live/{camera_id}/stream-ticket")
async def create_stream_ticket(
    camera_id: str,
    _user: dict = Depends(get_current_user),
):
    """Issue a one-use short-lived ticket for browser MJPEG loading."""
    validate_identifier(camera_id, name="camera_id")
    ticket = secrets.token_urlsafe(32)
    with _lock:
        now = time.monotonic()
        for key, (_, expiry) in list(_stream_tickets.items()):
            if expiry <= now:
                _stream_tickets.pop(key, None)
        _stream_tickets[ticket] = (camera_id, now + _STREAM_TICKET_TTL)
    return {"stream_token": ticket, "expires_in": int(_STREAM_TICKET_TTL)}


@router.get("/live/{camera_id}/stream.mjpg")
async def live_stream_mjpg(
    camera_id: str,
    fps: float | None = Query(default=None, ge=1.0, le=60.0),
    stream_token: str | None = Query(default=None),
    cred: HTTPAuthorizationCredentials | None = Depends(security),
):
    validate_identifier(camera_id, name="camera_id")
    _authorize_live_request(cred, stream_token, camera_id)
    _cleanup_stale_streams()

    # Reuse always-on pipeline if present; otherwise start a viewer-owned one.
    if not ensure_live_pipeline(camera_id, always_on=_env_auto_start_live()):
        return JSONResponse(
            {"detail": f"Live pipeline could not start for {camera_id}"},
            status_code=503,
        )

    with _lock:
        if camera_id not in _streams:
            return JSONResponse(
                {"detail": f"Live pipeline not available for {camera_id}"},
                status_code=503,
            )
        core, queue, meta, sw, sess, stop_ev = _streams[camera_id]
        meta["connections"] = meta.get("connections", 0) + 1
        meta["last_access"] = time.monotonic()
        # Prefer paced preview target (smooth) over raw source metadata FPS.
        target_fps = float(
            fps
            or meta.get("preview_fps_target")
            or meta.get("source_fps")
            or 25.0
        )

    return StreamingResponse(
        _mjpeg_generator(camera_id, queue, fps=target_fps, stop_event=stop_ev),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )
