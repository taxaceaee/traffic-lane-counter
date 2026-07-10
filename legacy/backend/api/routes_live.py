"""Live stream API — MJPEG endpoint for annotated camera frames.

24/7 operational hardening:
- Camera auto-reconnection with exponential backoff
- Thread-safe stream registry
- Resource limits: max cameras, stale stream cleanup
- Graceful degradation: returns last good frame when stream stalls
- Real-time DB persistence: crossing events stored to DB via StorageWorker
"""

import logging
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

import cv2
import numpy as np
import yaml
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from shared.config.loader import normalize_camera_config
from shared.detection_core import DetectionCore
from shared.roi import CropROI

# Try to import MotionDetector — lives in backend.detection and can save
# 40-60 % GPU on static-scene cameras (night / empty parking).
try:
    from backend.detection.motion_detector import MotionDetector as _MotionDetector
except ImportError:
    _MotionDetector = None

logger = logging.getLogger("trafficflow.live_api")

router = APIRouter(tags=["live"])

_MAX_STREAMS = 16
_STREAM_CLEANUP_INTERVAL = 300  # 5 min
_MAX_RECONNECT_ATTEMPTS = 10
_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_MAX = 60.0

# Each stream holds: (DetectionCore, Queue, meta_dict, StorageWorker, session, stop_event)
_streams: dict[str, tuple[DetectionCore, Queue, dict[str, Any], Any, Any, threading.Event]] = {}

# Per-camera timing accumulator for pipeline component profiling
_perf_timings: dict[str, dict[str, float]] = {}


def _log_timing(camera_id: str, label: str, duration_ms: float) -> None:
    """Accumulate per-component timing for the current frame.

    Aggregated timings are consumed in _capture_loop and passed to
    ``record_frame()`` for metrics tracking.
    """
    _perf_timings.setdefault(camera_id, {})[label] = duration_ms
_lock = threading.Lock()
_last_cleanup = time.monotonic()

# Latest full frame (pre-ROI-crop) per camera for snapshot reuse.
_last_full_frames: dict[str, np.ndarray] = {}


def _cleanup_stale_streams():
    """Remove streams that have no active MJPEG consumers."""
    global _last_cleanup
    now = time.monotonic()
    if now - _last_cleanup < _STREAM_CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    with _lock:
        dead = [cid for cid, (core, q, meta, sw, sess, stop_ev) in _streams.items()
                if meta.get("connections", 0) <= 0
                and now - meta.get("last_access", 0) > 120]
        for cid in dead:
            core, q, meta, sw, sess, stop_ev = _streams.pop(cid)
            stop_ev.set()  # signal capture thread to stop
            _last_full_frames.pop(cid, None)
            if sw is not None:
                try:
                    sw.stop(timeout=3.0)
                except Exception:
                    logger.warning("Failed to stop StorageWorker for %s", cid, exc_info=True)
            if sess is not None:
                try:
                    sess.close()
                except Exception:
                    pass
            logger.info("Cleaned up stale stream: %s", cid)


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
        if not lanes_path.exists():
            logger.warning("Lanes file not found for %s: %s — using empty lanes",
                           cfg.get("camera_id", "?"), lanes_path)
            return []
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

    Missing fields fall back to global defaults from settings.json.
    """
    model_id = model_section.get("model_id", "yolo11n_coco")
    # Resolve weights from models registry
    weights = "yolo11s.pt"
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

    # Merge: camera YAML values win, settings.json fills the rest
    try:
        from backend.services.settings_service import get_detection_defaults
        defaults = get_detection_defaults()
    except Exception:
        defaults = {}

    return {
        "weights": weights,
        "imgsz": model_section.get("imgsz", defaults.get("imgsz", 640)),
        "conf": model_section.get("conf_threshold", defaults.get("confidence", 0.35)),
        "iou": model_section.get("iou_threshold", defaults.get("iou", 0.5)),
        "class_mode": class_mode,
        "allowed_classes": model_section.get("allowed_classes", []),
        "half": model_section.get("half", defaults.get("half", True)),
        "detect_every_n_frames": model_section.get("detect_every_n_frames", defaults.get("detect_every_n_frames", 2)),
        "roi_crop": model_section.get("roi_crop", defaults.get("roi_crop", True)),
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
        "roi_padding": detector.get("roi_padding", 50),
        "lanes": lanes,
    }


def _start_pipeline(camera_id: str, cfg: dict[str, Any]) -> tuple[DetectionCore, Queue, Any, Any, threading.Event]:
    """Start a live detection pipeline with DB persistence and Redis publishing.

    Returns
    -------
    tuple of (DetectionCore, annotated_queue, StorageWorker, db_session, stop_event)
    """
    pipeline_cfg = _cfg_to_pipeline_dict(cfg, camera_cfg_path=Path(f"configs/cameras/{camera_id}.yaml"))
    lanes = pipeline_cfg["lanes"]
    source = cfg.get("source") or cfg.get("server", {}).get("source", "")
    source_type = cfg.get("source_type") or cfg.get("input", {}).get("source_type", "video")

    # ROI crop: compute crop region from detection zones (preferred) or
    # lane polygons (fallback). Detection runs only on the cropped area:
    # smaller → faster, fewer false positives.
    roi = None
    roi_crop_enabled = pipeline_cfg.get("detector", {}).get("roi_crop", True)
    if roi_crop_enabled and pipeline_cfg.get("lanes"):
        roi_padding = int(pipeline_cfg.get("roi_padding", 50))
        try:
            # Load zone config to see if user-defined zones exist
            zone_polygons = _load_zones(cfg)

            # Save original frame size BEFORE transform_config overwrites it
            _original_frame_w = pipeline_cfg["frame_size"]["width"]
            _original_frame_h = pipeline_cfg["frame_size"]["height"]

            roi = CropROI(
                pipeline_cfg["lanes"],
                pipeline_cfg["frame_size"],
                padding=roi_padding,
                zone_polygons=zone_polygons,  # None → falls back to lane polygons
            )
            pipeline_cfg = roi.transform_config(pipeline_cfg)
            # Override imgsz to match crop size — avoids wasteful
            # up-scaling of small crops and lossy down-scaling of
            # large crops.  The image is already at a good resolution
            # for the ROI.
            pipeline_cfg.setdefault("detector", {})
            pipeline_cfg["detector"]["imgsz"] = roi.suggested_imgsz()
            logger.info(
                "ROI crop: %s (area ratio: %.2f%%), imgsz=%d",
                roi, roi.area_ratio * 100,
                pipeline_cfg["detector"]["imgsz"],
            )
        except Exception:
            logger.warning("Failed to init CropROI — full frame", exc_info=True)
            roi = None
    else:
        # No ROI crop — use the frame size from config directly
        _original_frame_w = pipeline_cfg["frame_size"]["width"]
        _original_frame_h = pipeline_cfg["frame_size"]["height"]

    core = DetectionCore(pipeline_cfg)
    core.start()

    # ── DB persistence ──────────────────────────────────────────────────────
    from backend.db.session import SessionLocal
    from backend.storage.storage_worker import StorageWorker
    from backend.storage_adapters import make_server_adapters
    from backend.pubsub import RedisPublisher

    db_session = SessionLocal()
    adapter = make_server_adapters(db_session)

    import os
    redis_enabled = os.getenv("REDIS_HOST") is not None
    publisher = RedisPublisher(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
    ) if redis_enabled else None

    from backend.services.live_bus import LiveEventBus

    storage_root = Path("storage") / camera_id
    storage_root.mkdir(parents=True, exist_ok=True)
    storage_worker = StorageWorker(
        storage_root=storage_root,
        adapter=adapter,
        publisher=publisher,
    )

    # annotated_queue: output buffer for MJPEG generator.
    # Increased from 1 → 5 to smooth bursts: encode thread can push ahead
    # even when the browser TCP window is momentarily full.
    annotated_queue: Queue = Queue(maxsize=5)
    stop_event = threading.Event()

    # Dedicated encode thread with bounded work queue — offloads cv2.imencode
    # (~15-25ms per 1280x720 frame) so the capture loop never blocks on it.
    # The work queue is sized to absorb the burst pattern of detect_every_n:
    #   3 non-detect frames arrive in ~24 ms (8 ms each) but encode takes
    #   8-12 ms/frame.  A maxsize of 2 would drop the 3rd burst frame.
    #   maxsize=10 ensures zero drops during normal bursts.
    _encode_work_q: Queue = Queue(maxsize=10)

    def _encode_worker():
        """Background thread: JPEG-encode annotated frames from the work queue."""
        while not stop_event.is_set():
            try:
                to_encode = _encode_work_q.get(timeout=0.5)
            except Empty:
                continue
            try:
                # Quality 50 (down from 65) — the annotated frame is viewed
                # at stream resolution, not archival quality. This halves the
                # encode time and cuts bandwidth by ~40%.
                _, jpeg = cv2.imencode(".jpg", to_encode, [cv2.IMWRITE_JPEG_QUALITY, 50])
                annotated_queue.put_nowait(jpeg.tobytes())
            except (ValueError, Empty, Full):
                pass

    threading.Thread(target=_encode_worker, daemon=True,
                     name=f"enc-{camera_id}").start()

    # ── Event publisher worker: offloads LiveEventBus.publish() from the
    # capture loop so it never blocks on subscriber callbacks.
    _event_q: Queue = Queue(maxsize=64)

    def _event_publisher():
        while not stop_event.is_set():
            try:
                cid, data = _event_q.get(timeout=0.5)
                LiveEventBus.publish(cid, data)
            except Empty:
                continue
            except Exception:
                logger.debug("Event pub failed", exc_info=True)

    threading.Thread(target=_event_publisher, daemon=True,
                     name=f"evt-{camera_id}").start()

    # Adaptive MotionDetector: skips BGR→GRAY conversion on busy scenes
    # (every frame has motion for _MOTION_SKIP_AFTER frames).
    # Passing source_type so YouTube/RTSP sources auto-disable motion detection
    # (compression artifacts cause false motion on every frame).
    _motion_detector = _MotionDetector(threshold=0.03, source_type=source_type) if _MotionDetector is not None else None
    _motion_consecutive = 0
    _motion_inhibit = 0
    _MOTION_SKIP_AFTER = 10

    # Annotation skip: every 2nd non-detect frame skips bbox drawing
    _annotate_counter = 0

    _last_det: dict[str, Any] | None = None

    def _capture_loop():
        nonlocal _last_det, _motion_consecutive, _motion_inhibit, _annotate_counter
        attempt = 0
        while not stop_event.is_set():
            try:
                from backend.io.video_io import get_video_reader

                # Build video reader config: start with full input section from camera YAML
                # (which may contain yt_format, yt_refresh_interval, buffer_size, etc.),
                # then override source_type and fps so defaults always apply.
                reader_cfg = dict(cfg.get("input", {}))
                reader_cfg["source_type"] = source_type
                reader_cfg.setdefault("fps", cfg.get("fps", 25))

                reader = get_video_reader(
                    source,
                    {"input": reader_cfg},
                )
                attempt = 0  # reset after successful connect
                # Resolve camera_offline alert (only if a previous offline was emitted)
                if attempt > 0 or getattr(_capture_loop, "_was_offline", False):
                    from backend.services.alert_service import alert_service as _as
                    _as.resolve("camera_offline", camera_id)
                _capture_loop._was_offline = False
            except (OSError, ValueError, RuntimeError):
                attempt += 1
                if attempt > _MAX_RECONNECT_ATTEMPTS:
                    logger.error("Camera %s: max reconnection attempts reached", camera_id)
                    # Emit camera_offline alert
                    from backend.services.alert_service import alert_service as _as
                    _as.emit("critical", f"Camera Offline — {camera_id}",
                             f"Connection lost after {_MAX_RECONNECT_ATTEMPTS} attempts. Check source or network.",
                             camera_id=camera_id, alert_type="camera_offline")
                    _capture_loop._was_offline = True
                    break
                backoff = min(
                    _RECONNECT_BACKOFF_BASE * (2 ** (attempt - 1)),
                    _RECONNECT_BACKOFF_MAX,
                )
                logger.warning(
                    "Camera %s: reconnection attempt %d/%d in %.1fs",
                    camera_id, attempt, _MAX_RECONNECT_ATTEMPTS, backoff,
                )
                time.sleep(backoff)
                continue

            frame_idx = 0
            read_failures = 0
            try:
                while not stop_event.is_set():
                    success, frame = reader.read()
                    if not success or frame is None:
                        read_failures += 1
                        if read_failures > 30:  # ~3 seconds of read failures
                            logger.warning("Camera %s: too many read failures, reconnecting", camera_id)
                            break
                        time.sleep(0.1)
                        continue
                    read_failures = 0

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

                    # Expose full pipeline frame for external snapshot reuse
                    _last_full_frames[camera_id] = pipeline_frame

                    # 2. ROI crop on pipeline-sized copy
                    frame_roi = roi.crop(pipeline_frame) if roi is not None else pipeline_frame

                    # Motion-gated detection — skips YOLO entirely when nothing moves.
                    # The MotionDetector uses a cheap 160 px frame-differencing check
                    # (< 0.5 ms per frame) and when no motion is detected we reuse the
                    # last detection result.  On busy scenes detection runs every frame.
                    # Adaptive skip: on busy scenes, skip the BGR→GRAY conversion
                    # for _MOTION_SKIP_AFTER frames.
                    t_detect_start = time.perf_counter()
                    det = None
                    _det_is_fresh = False  # True only when core.process_frame() ran fresh
                    if _motion_inhibit > 0:
                        _motion_inhibit -= 1
                        # skip motion check — always run fresh detection (det stays None)
                    elif _motion_detector is not None and _motion_detector.has_motion(frame_roi) is False:
                        _motion_consecutive = 0
                        # Reuse last detection result — no fresh inference
                        if _last_det is not None:
                            det = dict(_last_det)  # shallow copy to avoid mutation of cached _last_det
                            det["frame_idx"] = frame_idx
                            det["frame_timestamp"] = datetime.now(timezone.utc)
                            # Deep-copy nested lists so coordinate transforms don't corrupt _last_det
                            for field in ("tracks", "raw_detections", "frame_tracks"):
                                if field in det and det[field] is not None:
                                    det[field] = [dict(t) for t in det[field]]
                            for fld in ("crossings", "events", "timing_ms"):
                                if fld in det and det[fld] is not None and isinstance(det[fld], list):
                                    det[fld] = list(det[fld])
                    else:
                        _motion_consecutive += 1
                        if _motion_consecutive >= _MOTION_SKIP_AFTER:
                            _motion_consecutive = 0
                            _motion_inhibit = _MOTION_SKIP_AFTER
                    if det is None:  # first frame, no motion skip, or inhibit active
                        det = core.process_frame(frame_roi, frame_idx=frame_idx)
                        _last_det = det
                        _det_is_fresh = True
                    detect_ms = (time.perf_counter() - t_detect_start) * 1000
                    _log_timing(camera_id, "detect", detect_ms)

                    # 3. Transform coordinates from crop space → pipeline frame.
                    #    IMPORTANT: ONLY transform when detection just ran (_det_is_fresh).
                    #    _last_det already stores original-space bboxes; re-transforming
                    #    them would produce garbage coordinates (P0 correctness bug).
                    #    We also NEVER mutate core.state_manager.track_states — they
                    #    must stay in crop-space for internal tracking consistency.
                    if roi is not None and _det_is_fresh:
                        fw, fh = _original_frame_w, _original_frame_h
                        for t in det.get("tracks", []):
                            if "bbox" in t:
                                t["bbox"] = roi.to_original(t["bbox"], fw, fh)
                        for r in det.get("raw_detections", []):
                            if "bbox" in r:
                                r["bbox"] = roi.to_original(r["bbox"], fw, fh)
                        for ft in det.get("frame_tracks", []):
                            if "bbox" in ft:
                                ft["bbox"] = roi.to_original(ft["bbox"], fw, fh)

                    # 4. Record metrics — merge DetectionCore timings (inference,
                    # preprocess, nms, tracking, ...) with pipeline-level timings
                    # (resize, annotate, encode_submit).
                    timing = dict(det.get("timing_ms", {}))
                    perf = _perf_timings.pop(camera_id, {})
                    timing.update(perf)
                    from backend.monitoring.live_metrics import record_frame
                    record_frame(camera_id, timing)

                    # frame_idx for persistence: use the value passed to process_frame
                    current_frame_idx = frame_idx

                    # ── Extract crossing crops from clean frame BEFORE annotation ──
                    crossings = det.get("crossings", [])
                    # Build a lookup of original-space bboxes from frame_tracks (already
                    # transformed in step 3, or deep-copied from _last_det for motion-skip).
                    _ft_bmap = {t.get("track_id"): t.get("bbox") for t in det.get("frame_tracks", [])
                                if t.get("bbox") and len(t["bbox"]) >= 4}
                    crop_data: list[tuple[str, int, str, str, float, list[float] | None, bytes | None]] = []
                    for cx in crossings:
                        track_id = cx.get("track_id")
                        # Use original-space bbox from frame_tracks (safe for cropping
                        # pipeline_frame). Fallback: transform state.bbox on-the-fly.
                        bbox = _ft_bmap.get(track_id)
                        if bbox is None and roi is not None:
                            state = core.state_manager.track_states.get(track_id)
                            if state is not None and state.bbox is not None and len(state.bbox) == 4:
                                # state.bbox is in crop-space — transform to original for crop extraction
                                bbox = roi.to_original(list(state.bbox), _original_frame_w, _original_frame_h)
                        elif bbox is None:
                            state = core.state_manager.track_states.get(track_id)
                            bbox = list(state.bbox) if state is not None and state.bbox is not None else None
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

                    # 5. Annotate in-place — skip OpenCV drawing on every other
                    #    non-detect frame to save ~4ms (50% of annotation cost).
                    _annotate_counter += 1
                    _skip_annotate = (_annotate_counter % 2 == 0)
                    t_annotate = time.perf_counter()
                    annotated = _annotate(pipeline_frame, det, lanes,
                                          skip_bbox=_skip_annotate)
                    _log_timing(camera_id, "annotate", (time.perf_counter() - t_annotate) * 1000)

                    # ── Push frame to browser via encode worker thread ──
                    # No .copy() needed: pipeline_frame is either a cv2.resize
                    # result or a fresh frame from the reader — in either case
                    # it's a new allocation each iteration. The encode thread
                    # holds the only remaining reference after reassignment.
                    t_submit = time.perf_counter()
                    try:
                        _encode_work_q.put_nowait(annotated)
                    except Full:
                        pass  # encode worker is backlogged — skip this frame
                    _log_timing(camera_id, "encode_submit", (time.perf_counter() - t_submit) * 1000)

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
                            frame_id=current_frame_idx,
                            timestamp=datetime.now(timezone.utc),
                            frame=None,
                            bbox=bbox,
                            crop_bytes=crop_bytes,
                        )

                    # ── Persist lane-change events to DB ──────────────
                    lane_events = det.get("events", [])
                    for le in lane_events:
                        try:
                            adapter.lane_changes.insert_event({
                                "camera_id": camera_id,
                                "track_id": le.get("track_id", -1),
                                "class_name": le.get("class_name", "unknown"),
                                "previous_lane_id": le.get("previous_stable_lane"),
                                "current_lane_id": le.get("current_stable_lane", "unknown"),
                                "frame_id": le.get("frame", current_frame_idx),
                            })
                        except Exception:
                            logger.warning("Failed to persist lane-change event for camera %s: track %s",
                                           camera_id, le.get("track_id", "?"), exc_info=True)
                    # Batch flush after all lane-change events
                    try:
                        adapter.lane_changes.flush()
                    except Exception:
                        logger.warning("Lane-change batch flush failed for %s", camera_id, exc_info=True)

                    # 6. Increment frame counter AFTER all persistence uses it
                    frame_idx += 1

                    # ── Publish events via async worker thread ────────
                    _evt = det.get("occupancy", {})
                    if _evt:
                        try:
                            _event_q.put_nowait((camera_id, {
                                "type": "occupancy_update",
                                "camera_id": camera_id,
                                "data": {
                                    "occupancy": _evt,
                                    "frame_idx": frame_idx,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                },
                            }))
                        except Full:
                            pass
                        # Redis publish stays sync (network i/o is async-compatible
                        # with bounded timeout and happens outside the lock)
                        if publisher is not None:
                            try:
                                publisher.publish_live_state(camera_id, {
                                    "occupancy": _evt,
                                    "frame_idx": frame_idx,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                })
                            except Exception:
                                logger.debug("Redis pub failed for %s", camera_id, exc_info=True)
                    for cx in crossings:
                        try:
                            _event_q.put_nowait((camera_id, {
                                "type": "count_event",
                                "camera_id": camera_id,
                                "data": cx,
                            }))
                        except Full:
                            pass
                    # Publish lane-change events to WebSocket
                    for le in lane_events:
                        try:
                            _event_q.put_nowait((camera_id, {
                                "type": "lane_change_event",
                                "camera_id": camera_id,
                                "data": le,
                            }))
                        except Full:
                            pass

            finally:
                reader.release()

    threading.Thread(target=_capture_loop, daemon=True, name=f"cam-{camera_id}").start()
    return core, annotated_queue, storage_worker, db_session, stop_event


def _annotate(frame: np.ndarray, det: dict[str, Any], lanes: list[dict],
              skip_bbox: bool = False) -> np.ndarray:
    """Annotate frame with lanes, tracks, and occupancy panel.

    When *skip_bbox* is True, tracks/boxes are NOT drawn — only
    counting lines, lane polygons, and the occupancy panel remain.
    This saves ~4ms on every other non-detect frame.
    """
    canvas = frame  # annotate in-place — saves ~6 MB copy per frame

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
        pts = np.array(lane_data["points"], dtype=np.int32).reshape((-1, 1, 2))
        cv2.polylines(canvas, [pts], isClosed=True, color=(255, 220, 0), thickness=2)
        first_pt = lane_data["points"][0]
        cv2.putText(canvas, lane_data["id"], (int(first_pt[0]) + 5, int(first_pt[1]) - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.putText(canvas, lane_data["id"], (int(first_pt[0]) + 5, int(first_pt[1]) - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 220, 0), 1)

    if not skip_bbox:
        ftracks = det.get("frame_tracks", [])
        for t in ftracks:
            bbox = t.get("bbox")
            if not bbox or len(bbox) < 4:
                continue
            x1, y1, x2, y2 = [int(v) for v in bbox]
            counted = t.get("is_counted_in_occupancy", False)
            box_color = (0, 255, 127) if counted else (100, 100, 100)
            cv2.rectangle(canvas, (x1, y1), (x2, y2), box_color, 2)
            cx = (x1 + x2) // 2
            cv2.circle(canvas, (cx, y2), 5, (0, 0, 255), -1)
            stable = t.get("stable_lane") or "none"
            label = f"#{t['track_id']} {t['class_name']} ({stable})"
            cv2.putText(canvas, label, (x1 + 3, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

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
            cv2.putText(canvas, f"{lane_id}:", (25, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1)
            cv2.putText(canvas, str(occupancy[lane_id]), (15 + panel_w - 40, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 127), 2)
            y += 25

    for cx in crossings[:1]:
        cv2.putText(canvas, f"{cx['class_name']} #{cx['track_id']} {cx['direction']}",
                    (20, canvas.shape[0] - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)

    return canvas


def _mjpeg_generator(queue: Queue, fps: float = 25.0):
    """MJPEG streaming generator — yields pre-encoded JPEG bytes at a steady FPS.

    Instead of yielding frames as fast as they arrive (which causes stutter
    when bursts arrive between quiet periods), this generator uses a steady
    timer: it drains the encode queue to get the *latest* frame, then sleeps
    until the next scheduled yield time.  This guarantees a constant frame
    rate at the browser regardless of encode timing jitter.

    Anti-stutter design:
      - If the encode thread is ahead (burst after detect_every_n skip), we
        just take the latest frame and wait — no burst yield.
      - If the encode thread is behind (slow GPU), we re-send last_jpeg and
        skip missing slots — no stale-frame stall count pollution.
      - ``queue.maxsize=5`` upstream gives enough buffer to absorb 3-frame
        bursts without dropping anything.
    """
    interval = 1.0 / max(fps, 1.0)
    last_jpeg: bytes | None = None
    stall_count = 0
    _no_signal: bytes | None = None
    next_yield = time.monotonic() + interval

    # Pre-compute MJPEG boundary bytes once
    _boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
    _trailer = b"\r\n"

    while True:
        # Drain the queue to get the *latest* frame (discard stale ones)
        jpeg_bytes = None
        while True:
            try:
                jpeg_bytes = queue.get_nowait()
            except Empty:
                break

        if jpeg_bytes is not None:
            last_jpeg = jpeg_bytes
            stall_count = 0

        # Sleep exactly until the next scheduled yield slot.
        # If we miss the slot (sleep_time < 0), do NOT accumulate drift:
        # schedule the next yield at now + interval instead of next_yield + interval.
        now = time.monotonic()
        sleep_time = next_yield - now
        if sleep_time > 0:
            time.sleep(sleep_time)
            next_yield = next_yield + interval
        else:
            # Missed the slot — reset clock to avoid drift accumulation
            next_yield = now + interval

        if last_jpeg is not None:
            yield _boundary + last_jpeg + _trailer
        else:
            stall_count += 1
            if stall_count > 250:
                if _no_signal is None:
                    blank = np.zeros((540, 960, 3), dtype=np.uint8)
                    cv2.putText(blank, "NO SIGNAL",
                                (960 // 4, 540 // 2),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                                (100, 100, 100), 3)
                    _, enc = cv2.imencode(".jpg", blank,
                                          [cv2.IMWRITE_JPEG_QUALITY, 60])
                    _no_signal = enc.tobytes()
                yield _boundary + _no_signal + _trailer


def _cleanup_stream(camera_id: str) -> bool:
    """Forcefully stop and remove a live pipeline stream. Returns True if existed."""
    with _lock:
        if camera_id not in _streams:
            return False
        core, q, meta, sw, sess, stop_ev = _streams.pop(camera_id)
    stop_ev.set()
    _last_full_frames.pop(camera_id, None)
    if sw is not None:
        try:
            sw.stop(timeout=3.0)
        except Exception:
            pass
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass
    from backend.monitoring.live_metrics import record_stream_stopped
    record_stream_stopped(camera_id)
    logger.info("Cleaned up stream: %s", camera_id)
    return True


@router.post("/live/{camera_id}/reload")
async def reload_live_pipeline(camera_id: str, model_id: str | None = None):
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
            with open(cam_yaml, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            raw.setdefault("model", {})["model_id"] = model_id
            with open(cam_yaml, "w", encoding="utf-8") as f:
                yaml.safe_dump(raw, f, sort_keys=False)

    _cleanup_stream(camera_id)
    return {"status": "reloaded", "camera_id": camera_id, "model_id": model_id}


@router.get("/live/{camera_id}/metrics")
async def live_camera_metrics(camera_id: str):
    """Return real-time FPS, latency, and GPU metrics for a live camera stream."""
    from backend.monitoring.live_metrics import get_camera_metrics
    return get_camera_metrics(camera_id)


@router.get("/live/{camera_id}/stream.mjpg")
async def live_stream_mjpg(
    camera_id: str,
    fps: float = Query(25.0, ge=1.0, le=60.0),
):
    _cleanup_stale_streams()

    with _lock:
        if camera_id not in _streams:
            if len(_streams) >= _MAX_STREAMS:
                return JSONResponse(
                    {"detail": "Maximum concurrent streams reached"},
                    status_code=503,
                )
            cfg = _load_camera_config(camera_id)
            if cfg is None:
                return JSONResponse(
                    {"detail": f"Camera not found: {camera_id}"},
                    status_code=404,
                )
            try:
                core, queue, sw, sess, stop_ev = _start_pipeline(camera_id, cfg)
            except Exception as e:
                logger.error("Failed to start pipeline for %s: %s", camera_id, e, exc_info=True)
                return JSONResponse(
                    {"detail": f"Failed to start live pipeline: {e}"},
                    status_code=500,
                )
            _streams[camera_id] = (core, queue, {"connections": 0, "last_access": time.monotonic()}, sw, sess, stop_ev)

        core, queue, meta, sw, sess, stop_ev = _streams[camera_id]
        meta["connections"] = meta.get("connections", 0) + 1
        meta["last_access"] = time.monotonic()

    return StreamingResponse(
        _mjpeg_generator(queue, fps=fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
