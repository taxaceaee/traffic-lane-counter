"""DetectionEngine — manages per-camera DetectionCore instances with hot-update support.

One DetectionCore per (camera_id) so that ByteTrack Kalman state, lane state
manager history, and internal frame_idx are isolated per camera.

Lanes are dynamic — they come from the request payload, not from files.
When lanes change, hot-update is used (polygons swapped in-place without
resetting track state). Full rebuild only happens when model/imgsz/thresholds change.
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Any

import numpy as np

from detection_server.schemas.detect import DetectRequest, DetectResponse, TimingMs

logger = logging.getLogger("detection_server.engine")


class DetectionEngine:
    """Thread-safe registry of DetectionCore instances per camera_id.

    Also handles ROI cropping: the request includes an optional roi field;
    frames are cropped before inference and bboxes are mapped back to original
    frame coordinates in the response.
    """

    def __init__(self):
        self._cores: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, request: DetectRequest, frame: np.ndarray) -> DetectResponse:
        """Run detection on a single BGR frame.

        Uses hot-update when only lanes change (no track state reset).
        Full rebuild only when model/imgsz/thresholds change.

        Parameters
        ----------
        request:
            DetectRequest with camera_id, model, lanes, roi, thresholds etc.
        frame:
            BGR numpy array (H, W, 3).

        Returns
        -------
        DetectResponse with tracks, occupancy, crossings, timing.
        """
        config_hash = self._config_hash(request)
        camera_id = request.camera_id

        with self._lock:
            existing = self._cached_config.get(camera_id)
            if existing != config_hash:
                # Decide: hot-update (lanes only) or full rebuild
                if self._is_lanes_only_change(camera_id, request):
                    logger.debug("Hot-update lanes for camera %s", camera_id)
                    lanes_data = self._build_lanes_config(request.lanes)
                    self._cores[camera_id].update_lanes(lanes_data)
                else:
                    self._rebuild(camera_id, request)
                self._cached_config[camera_id] = config_hash

        core = self._cores.get(camera_id)
        if core is None:
            # First request ever — rebuild is deferred to here
            with self._lock:
                if camera_id not in self._cores:
                    self._rebuild(camera_id, request)
                    self._cached_config[camera_id] = config_hash
                core = self._cores[camera_id]

        frame_timestamp = datetime.now(timezone.utc)

        # ── ROI crop ─────────────────────────────────────────────────────
        if request.roi is not None:
            roi = request.roi
            h, w = frame.shape[:2]
            x1 = max(0, roi.x1)
            y1 = max(0, roi.y1)
            x2 = min(w, roi.x2)
            y2 = min(h, roi.y2)
            frame_cropped = frame[y1:y2, x1:x2].copy()
            result = core.process_frame(frame_cropped, frame_timestamp=frame_timestamp)
            # Map bboxes back to original frame
            for track in result.get("tracks", []):
                bbox = track.get("bbox")
                if bbox and len(bbox) == 4:
                    bbox[0] += x1
                    bbox[1] += y1
                    bbox[2] += x1
                    bbox[3] += y1
            for rd in result.get("raw_detections", []):
                bbox = rd.get("bbox")
                if bbox and len(bbox) == 4:
                    bbox[0] += x1
                    bbox[1] += y1
                    bbox[2] += x1
                    bbox[3] += y1
            for ft in result.get("frame_tracks", []):
                bbox = ft.get("bbox")
                if bbox and len(bbox) == 4:
                    bbox[0] += x1
                    bbox[1] += y1
                    bbox[2] += x1
                    bbox[3] += y1
        else:
            result = core.process_frame(frame, frame_timestamp=frame_timestamp)

        # ── Build response ───────────────────────────────────────────────
        return DetectResponse(
            camera_id=camera_id,
            frame_idx=result["frame_idx"],
            frame_timestamp=result["frame_timestamp"].isoformat(),
            tracks=[
                {
                    "track_id": t["track_id"],
                    "class_name": t["class_name"],
                    "confidence": t["confidence"],
                    "bbox": t["bbox"],
                    "lane_id": t.get("lane_id"),
                }
                for t in result["tracks"]
            ],
            raw_detections=[
                {"class_name": d["class_name"], "confidence": d["confidence"], "bbox": d["bbox"]}
                for d in result.get("raw_detections", [])
            ],
            events=[
                {
                    "frame": e["frame"],
                    "track_id": e["track_id"],
                    "class_name": e["class_name"],
                    "previous_stable_lane": e["previous_stable_lane"],
                    "current_stable_lane": e["current_stable_lane"],
                }
                for e in result.get("events", [])
            ],
            occupancy=result.get("occupancy", {}),
            crossings=[
                {
                    "frame": c["frame"],
                    "track_id": c["track_id"],
                    "class_name": c["class_name"],
                    "lane_id": c["lane_id"],
                    "line_id": c["line_id"],
                    "direction": c["direction"],
                    "confidence": c["confidence"],
                }
                for c in result.get("crossings", [])
            ],
            frame_tracks=[
                {
                    "track_id": ft["track_id"],
                    "class_name": ft["class_name"],
                    "confidence": ft["confidence"],
                    "bbox": ft["bbox"],
                    "raw_lane": ft["raw_lane"],
                    "stable_lane": ft["stable_lane"],
                    "is_counted_in_occupancy": ft["is_counted_in_occupancy"],
                }
                for ft in result.get("frame_tracks", [])
            ],
            timing_ms=TimingMs(
                detect_track=result["timing_ms"]["detect_track"],
                lane_assign=result["timing_ms"]["lane_assign"],
                occupancy=result["timing_ms"]["occupancy"],
                counting=result["timing_ms"]["counting"],
            ),
        )

    def get_status(self, camera_id: str) -> dict | None:
        """Return quick status for a camera."""
        core = self._cores.get(camera_id)
        if core is None:
            return None
        return {
            "camera_id": camera_id,
            "frame_idx": core.frame_idx,
            "track_count": len(core.state_manager.track_states) if core.state_manager else 0,
        }

    def list_cameras(self) -> list[str]:
        """Return all active camera_ids."""
        with self._lock:
            return list(self._cores.keys())

    def reset(self, camera_id: str) -> None:
        """Reset DetectionCore state for a camera (e.g. after source change)."""
        with self._lock:
            if camera_id in self._cores:
                self._cores[camera_id].reset()
                logger.info("Reset DetectionCore for camera %s", camera_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    _cached_config: dict[str, str] = {}

    def _config_hash(self, req: DetectRequest) -> str:
        """Simple hash to detect config changes — enough for lane changes."""
        lane_ids = sorted(l.lane_id for l in req.lanes)
        return (
            f"{req.model_weights}|{req.imgsz}|{req.conf_threshold}|{req.iou_threshold}|"
            f"{req.half}|{req.detect_every_n_frames}|{req.min_track_age_frames}|"
            f"{req.min_cross_distance_px}|{','.join(lane_ids)}|"
            f"{req.roi.x1 if req.roi else 0},{req.roi.y1 if req.roi else 0},"
            f"{req.roi.x2 if req.roi else 0},{req.roi.y2 if req.roi else 0}"
        )

    def _is_lanes_only_change(self, camera_id: str, req: DetectRequest) -> bool:
        """Check if only lanes changed (vs model/threshold/etc.) — use hot-update."""
        core = self._cores.get(camera_id)
        if core is None:
            return False
        old_cfg = core.config
        det = old_cfg.get("detector", {})
        return (
            det.get("weights") == req.model_weights
            and det.get("imgsz") == req.imgsz
            and det.get("conf") == req.conf_threshold
            and det.get("iou") == req.iou_threshold
            and det.get("half") == req.half
            and det.get("detect_every_n_frames") == req.detect_every_n_frames
            and old_cfg.get("tracking", {}).get("min_track_age_frames") == req.min_track_age_frames
        )

    def _build_lanes_config(self, lanes) -> list[dict]:
        """Convert LaneDef list to the dict format DetectionCore.update_lanes() expects."""
        result = []
        for l in lanes:
            entry = {"id": l.lane_id, "points": l.polygon}
            if l.counting_line:
                entry["counting_line"] = l.counting_line.model_dump()
            result.append(entry)
        return result

    def _rebuild(self, camera_id: str, req: DetectRequest) -> None:
        from shared.detection_core import DetectionCore

        config = self._to_pipeline_config(req)
        core = DetectionCore(config)
        core.start()
        self._cores[camera_id] = core
        logger.info(
            "DetectionCore (re)built for camera %s: model=%s imgsz=%d lanes=%d",
            camera_id, req.model_weights, req.imgsz, len(req.lanes),
        )

    def _to_pipeline_config(self, req: DetectRequest) -> dict[str, Any]:
        frame_w = req.imgsz * 2  # generous default
        frame_h = req.imgsz
        if req.roi:
            frame_w = max(frame_w, req.roi.x2 - req.roi.x1)
            frame_h = max(frame_h, req.roi.y2 - req.roi.y1)

        lanes_config = []
        for lane in req.lanes:
            entry: dict[str, Any] = {
                "id": lane.lane_id,
                "points": lane.polygon,
            }
            if lane.counting_line:
                entry["counting_line"] = lane.counting_line.model_dump()
            lanes_config.append(entry)

        return {
            "camera_id": req.camera_id,
            "frame_size": {"width": frame_w, "height": frame_h},
            "coordinate_space": "original_frame",
            "detector": {
                "weights": req.model_weights,
                "imgsz": req.imgsz,
                "conf": req.conf_threshold,
                "iou": req.iou_threshold,
                "half": req.half,
                "allowed_classes": req.allowed_classes,
                "detect_every_n_frames": req.detect_every_n_frames,
            },
            "tracking": {
                "tracker": "bytetrack.yaml",
                "min_track_age_frames": req.min_track_age_frames,
            },
            "occupancy": {
                "history_window": 10,
                "min_consecutive_for_change": 5,
                "unknown_timeout_frames": 15,
            },
            "counting": {
                "min_cross_distance_px": req.min_cross_distance_px,
            },
            "lanes": lanes_config,
        }
