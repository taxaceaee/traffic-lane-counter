from typing import Any

import numpy as np

from tf_core.detection.yolo_detector import YoloDetectorWrapper


class YoloByteTrackAdapter:
    """Adapter to run Ultralytics YOLO with ByteTrack persistently frame-by-frame.

    Supports detect_every_n_frames: on skipped frames the model still runs
    model.track() with the same frame so ByteTrack can propagate its Kalman
    predictions — but we return the cached result from the last detection frame,
    avoiding re-running the expensive CNN backbone.
    """

    def __init__(self, detector: YoloDetectorWrapper, config: dict):
        self.detector = detector
        self.config = config
        self.detector_config = config.get("detector", {})
        self.tracking_config = config.get("tracking", {})

        self.imgsz = self.detector_config.get("imgsz", 960)
        self.conf = self.detector_config.get("conf", 0.35)
        self.iou = self.detector_config.get("iou", 0.5)
        self.tracker_config = self.tracking_config.get("tracker", "bytetrack.yaml")
        # Run full detection every N frames; intermediate frames use cached detections.
        # Set to 1 (default) to detect every frame.
        self.detect_every_n = max(1, self.detector_config.get("detect_every_n_frames", 1))

        self.names = self.detector.model.names
        self.allowed_classes = self.detector_config.get("allowed_classes", [])

        self.allowed_class_indices = []
        for idx, name in self.names.items():
            if name in self.allowed_classes:
                self.allowed_class_indices.append(idx)

        self._frame_count = 0
        self._cached_tracks: list[dict[str, Any]] = []
        self._cached_raw: list[dict[str, Any]] = []
        self._half_retry_disabled = False

    def _run_track(self, frame: np.ndarray):
        try:
            return self.detector.model.track(
                source=frame,
                persist=True,
                imgsz=self.imgsz,
                conf=self.conf,
                iou=self.iou,
                tracker=self.tracker_config,
                classes=self.allowed_class_indices if self.allowed_class_indices else None,
                verbose=False,
            )
        except RuntimeError as exc:
            err = str(exc)
            dtype_mismatch = "same dtype" in err or "Half !=" in err or "c10::Half" in err
            if not self.detector.half or self._half_retry_disabled or not dtype_mismatch:
                raise
            # Some CUDA/Ultralytics combinations fail when fusing a half-converted model.
            # Fall back to FP32 once so the live pipeline stays available.
            self._half_retry_disabled = True
            self.detector.half = False
            self.detector.model.model.float()
            predictor = getattr(self.detector.model, "predictor", None)
            if predictor is not None:
                predictor.model = None
            return self.detector.model.track(
                source=frame,
                persist=True,
                imgsz=self.imgsz,
                conf=self.conf,
                iou=self.iou,
                tracker=self.tracker_config,
                classes=self.allowed_class_indices if self.allowed_class_indices else None,
                verbose=False,
            )

    def track(self, frame: np.ndarray) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Runs detection+tracking on a single frame using one inference call.

        On frames that are not detection frames (detect_every_n_frames > 1) the
        neural network backbone is skipped and cached results are returned, reducing
        GPU load by up to 50% at detect_every_n_frames=2.

        Returns:
            tracks: list of active track dicts (track_id, class_name, confidence, bbox)
            raw_detections: same boxes derived from the single track call
        """
        self._frame_count += 1
        is_detect_frame = (self._frame_count % self.detect_every_n == 1) or self.detect_every_n == 1

        if not is_detect_frame:
            return self._cached_tracks, self._cached_raw

        results = self._run_track(frame)

        if not results:
            return [], []

        result = results[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return [], []

        xyxy = boxes.xyxy.cpu().numpy()
        confidences = boxes.conf.cpu().numpy()
        class_indices = boxes.cls.cpu().numpy().astype(int)

        # raw_detections derived from the same call — no second inference needed
        raw_detections = []
        for i in range(len(xyxy)):
            raw_detections.append({
                "bbox": xyxy[i].tolist(),
                "class_name": self.names.get(int(class_indices[i]), "unknown"),
                "confidence": float(confidences[i]),
            })

        track_ids = boxes.id
        if track_ids is None:
            self._cached_tracks, self._cached_raw = [], raw_detections
            return [], raw_detections

        track_ids = track_ids.cpu().numpy().astype(int)

        tracks = []
        for i in range(len(track_ids)):
            tracks.append({
                "track_id": int(track_ids[i]),
                "class_name": self.names.get(int(class_indices[i]), "unknown"),
                "confidence": float(confidences[i]),
                "bbox": xyxy[i].tolist(),
            })

        self._cached_tracks, self._cached_raw = tracks, raw_detections
        return tracks, raw_detections

