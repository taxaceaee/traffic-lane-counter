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

_MAX_STREAMS = 16
_STREAM_CLEANUP_INTERVAL = 300  # 5 min
_MAX_RECONNECT_ATTEMPTS = 10
_RECONNECT_BACKOFF_BASE = 1.0
_RECONNECT_BACKOFF_MAX = 60.0

# Each stream holds: (DetectionCore, Queue, meta_dict, StorageWorker, session, stop_event)
_streams: dict[str, tuple[DetectionCore, Queue, dict[str, Any], Any, Any, threading.Event]] = {}
_lock = threading.Lock()
_last_cleanup = time.monotonic()


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

    return {
        "weights": weights,
        "imgsz": model_section.get("imgsz", 640),
        "conf": model_section.get("conf_threshold", 0.35),
        "iou": model_section.get("iou_threshold", 0.5),
        "class_mode": class_mode,
        "allowed_classes": model_section.get("allowed_classes", []),
        "half": model_section.get("half", True),
        "detect_every_n_frames": model_section.get("detect_every_n_frames", 2),
        "roi_crop": model_section.get("roi_crop", True),
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
    from tf_db.session import SessionLocal
    from tf_worker.storage.storage_worker import StorageWorker
    from tf_api.storage_adapters import make_server_adapters
    from tf_common.pubsub import RedisPublisher

    db_session = SessionLocal()
    adapter = make_server_adapters(db_session)

    import os
    redis_enabled = os.getenv("REDIS_HOST") is not None
    publisher = RedisPublisher(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
    ) if redis_enabled else None

    from tf_common.live_bus import LiveEventBus

    storage_root = Path("storage") / camera_id
    storage_root.mkdir(parents=True, exist_ok=True)
    storage_worker = StorageWorker(
        storage_root=storage_root,
        adapter=adapter,
        publisher=publisher,
    )

    annotated_queue: Queue = Queue(maxsize=1)
    stop_event = threading.Event()

    # Dedicated encode thread with bounded work queue — offloads cv2.imencode
    # (~15-25ms per 1280x720 frame) so the capture loop never blocks on it.
    # The encode work queue is bounded (maxsize=2) so the capture thread drops
    # frames when encode can't keep up, preventing unbounded memory growth.
    _encode_work_q: Queue = Queue(maxsize=2)

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

    # MotionDetector — skips YOLO entirely on static frames (saves 40-60% GPU)
    _motion_detector = _MotionDetector(threshold=0.03) if _MotionDetector is not None else None
    _last_det: dict[str, Any] | None = None

    def _capture_loop():
        nonlocal _last_det
        attempt = 0
        while not stop_event.is_set():
            try:
                from tf_worker.io.video_io import get_video_reader

                reader = get_video_reader(
                    source,
                    {"input": {"source_type": source_type, "fps": cfg.get("fps", 25)}},
                )
                attempt = 0  # reset after successful connect
                # Resolve camera_offline alert (only if a previous offline was emitted)
                if attempt > 0 or getattr(_capture_loop, "_was_offline", False):
                    from tf_common.alert_service import alert_service as _as
                    _as.resolve("camera_offline", camera_id)
                _capture_loop._was_offline = False
            except (OSError, ValueError, RuntimeError):
                attempt += 1
                if attempt > _MAX_RECONNECT_ATTEMPTS:
                    logger.error("Camera %s: max reconnection attempts reached", camera_id)
                    # Emit camera_offline alert
                    from tf_common.alert_service import alert_service as _as
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

                    # 2. ROI crop on pipeline-sized copy
                    frame_roi = roi.crop(pipeline_frame) if roi is not None else pipeline_frame

                    # Motion-gated detection — skips YOLO entirely when nothing moves.
                    # The MotionDetector uses a cheap 160 px frame-differencing check
                    # (< 0.5 ms per frame) and when no motion is detected we reuse the
                    # last detection result.  On busy scenes detection runs every frame.
                    t_detect_start = time.perf_counter()
                    det = None
                    if _motion_detector is not None and _motion_detector.has_motion(frame_roi) is False:
                        det = _last_det
                        if det is not None:
                            det["frame_idx"] = frame_idx
                            det["frame_timestamp"] = datetime.now(timezone.utc)
                    if det is None:  # first frame, motion detected, or no motion detector
                        det = core.process_frame(frame_roi, frame_idx=frame_idx)
                        _last_det = det
                    frame_idx += 1
                    detect_ms = (time.perf_counter() - t_detect_start) * 1000
                    _log_timing(camera_id, "detect", detect_ms)

                    # 3. Transform coordinates from crop space → pipeline frame
                    if roi is not None:
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
                        for state in core.state_manager.track_states.values():
                            if state.bbox is not None and len(state.bbox) == 4:
                                state.bbox = roi.to_original(state.bbox, fw, fh)

                    # 4. Record metrics
                    timing = det.get("timing_ms", {})
                    from tf_common.monitoring.live_metrics import record_frame
                    record_frame(camera_id, timing)

                    # ── Extract crossing crops from clean frame BEFORE annotation ──
                    # This avoids annotating in-place then reading back annotated pixels
                    # for crop extraction.  Moving it earlier also parallelises work:
                    # the MJPEG queue push and StorageWorker enqueue can overlap.
                    crossings = det.get("crossings", [])
                    crop_data: list[tuple[str, int, str, str, float, list[float] | None, bytes | None]] = []
                    for cx in crossings:
                        state = core.state_manager.track_states.get(cx.get("track_id"))
                        bbox = state.bbox if state is not None else None
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

                    # 5. Annotate in-place on pipeline_frame (avoids ~6 MB copy per frame)
                    t_annotate = time.perf_counter()
                    annotated = _annotate(pipeline_frame, det, lanes)
                    _log_timing(camera_id, "annotate", (time.perf_counter() - t_annotate) * 1000)

                    # ── Push frame to browser via encode worker thread ──
                    # cv2.imencode on a 1280×720 frame takes 15-25ms.  Blocking
                    # the capture loop on encode would cut FPS in half.  Instead
                    # we push the annotated frame to the encode work queue and
                    # continue immediately with the next detect cycle.  The work
                    # queue is bounded (maxsize=2) so memory doesn't grow when
                    # the browser is slow to consume frames.
                    t_submit = time.perf_counter()
                    try:
                        # Copy before queuing — the encode thread runs in parallel
                        # and must not read frame data while the capture loop is
                        # overwriting it with the next frame.
                        _encode_work_q.put_nowait(annotated.copy())
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
                            frame_id=frame_idx,
                            timestamp=datetime.now(timezone.utc),
                            frame=None,  # never store full frame in queue — OOM risk
                            bbox=bbox,
                            crop_bytes=crop_bytes,
                        )

                    # ── Publish live occupancy snapshot ────────────────
                    occupancy = det.get("occupancy", {})
                    if occupancy:
                        # In-process bus (always available, no Redis needed)
                        LiveEventBus.publish(camera_id, {
                            "type": "occupancy_update",
                            "camera_id": camera_id,
                            "data": {
                                "occupancy": occupancy,
                                "frame_idx": frame_idx,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            },
                        })
                        # Redis (when available)
                        if publisher is not None:
                            try:
                                publisher.publish_live_state(camera_id, {
                                    "occupancy": occupancy,
                                    "frame_idx": frame_idx,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                })
                            except Exception:
                                logger.debug("Failed to publish live state for %s", camera_id, exc_info=True)

                    # Also publish crossing events as "count" events
                    for cx in crossings:
                        LiveEventBus.publish(camera_id, {
                            "type": "count_event",
                            "camera_id": camera_id,
                            "data": cx,
                        })

            finally:
                reader.release()

    threading.Thread(target=_capture_loop, daemon=True, name=f"cam-{camera_id}").start()
    return core, annotated_queue, storage_worker, db_session, stop_event


def _annotate(frame: np.ndarray, det: dict[str, Any], lanes: list[dict]) -> np.ndarray:
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
    """MJPEG streaming generator — yields pre-encoded JPEG bytes from the capture thread.

    Uses blocking ``queue.get(timeout=interval)`` so frames are served
    *immediately* when the capture thread enqueues them — no fixed ``time.sleep()``
    bottleneck.  On timeout the last known frame is re-sent (stale-frame tolerance).
    """
    interval = 1.0 / max(fps, 1.0)
    last_jpeg: bytes | None = None
    stall_count = 0
    # Pre-compute a "NO SIGNAL" placeholder (encoded once, reused on stalls).
    _no_signal: bytes | None = None

    while True:
        try:
            jpeg_bytes = queue.get(timeout=interval)
        except Empty:
            jpeg_bytes = None

        if jpeg_bytes is None:
            if last_jpeg is not None:
                stall_count += 1
                if stall_count > 250:  # ~10s of stale frame (at interval rate)
                    if _no_signal is None:
                        blank = np.zeros((540, 960, 3), dtype=np.uint8)
                        cv2.putText(blank, "NO SIGNAL",
                                    (960 // 4, 540 // 2),
                                    cv2.FONT_HERSHEY_SIMPLEX, 1.5,
                                    (100, 100, 100), 3)
                        _, enc = cv2.imencode(".jpg", blank,
                                              [cv2.IMWRITE_JPEG_QUALITY, 60])
                        _no_signal = enc.tobytes()
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + _no_signal + b"\r\n"
                    )
                else:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" + last_jpeg + b"\r\n"
                    )
                continue
            else:
                # No frame ever received — busy-wait is fine, no consumer yet
                continue

        # Fresh frame — yielded immediately, no artificial sleep
        stall_count = 0
        last_jpeg = jpeg_bytes
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + jpeg_bytes + b"\r\n"
        )


def _cleanup_stream(camera_id: str) -> bool:
    """Forcefully stop and remove a live pipeline stream. Returns True if existed."""
    with _lock:
        if camera_id not in _streams:
            return False
        core, q, meta, sw, sess, stop_ev = _streams.pop(camera_id)
    stop_ev.set()
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
    from tf_common.monitoring.live_metrics import record_stream_stopped
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
    from tf_common.monitoring.live_metrics import get_camera_metrics
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
            core, queue, sw, sess, stop_ev = _start_pipeline(camera_id, cfg)
            _streams[camera_id] = (core, queue, {"connections": 0, "last_access": time.monotonic()}, sw, sess, stop_ev)

        core, queue, meta, sw, sess, stop_ev = _streams[camera_id]
        meta["connections"] = meta.get("connections", 0) + 1
        meta["last_access"] = time.monotonic()

    return StreamingResponse(
        _mjpeg_generator(queue, fps=fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
