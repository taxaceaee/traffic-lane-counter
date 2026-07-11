"""YOLO + ByteTrack adapter — one inference call yields tracks + raw boxes.

Realtime notes
--------------
* ``detect_every_n > 1`` skips the CNN on intermediate frames and reuses the
  last boxes (cheap). Prefer every_n=1 with a viewer; headless multi-cam uses 2.
* Empty detections clear the cache so ghost boxes do not linger.
* CUDA half + explicit device are set when available for throughput.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from tf_core.detection.yolo_detector import YoloDetectorWrapper

# Multi-cam always-on shares one YOLO via ModelRegistry. Ultralytics
# ``track(persist=True)`` is not re-entrant — serialize GPU forwards so
# concurrent capture threads do not thrash the shared predictor.
_INFERENCE_LOCK = threading.Lock()


class YoloByteTrackAdapter:
    """Run Ultralytics YOLO ``.track()`` with ByteTrack, frame by frame."""

    def __init__(self, detector: YoloDetectorWrapper, config: dict):
        self.detector = detector
        self.config = config
        self.detector_config = config.get("detector", {})
        self.tracking_config = config.get("tracking", {})

        self.imgsz = int(self.detector_config.get("imgsz", 960))
        # 0.20–0.25 is the practical recall/precision band for traffic cams.
        self.conf = float(self.detector_config.get("conf", 0.22))
        self.iou = float(self.detector_config.get("iou", 0.50))
        self.max_det = int(self.detector_config.get("max_detections", 300))
        self.tracker_config = self.tracking_config.get("tracker", "bytetrack.yaml")
        self.detect_every_n = max(1, int(self.detector_config.get("detect_every_n_frames", 1)))

        self.names = self.detector.model.names
        self.allowed_classes = self.detector_config.get("allowed_classes", [])

        self.allowed_class_indices: list[int] = []
        for idx, name in self.names.items():
            if name in self.allowed_classes:
                self.allowed_class_indices.append(idx)

        self._frame_count = 0
        self._cached_tracks: list[dict[str, Any]] = []
        self._cached_raw: list[dict[str, Any]] = []
        self._half_retry_disabled = False
        self._device: str | int = self._resolve_device()

    @staticmethod
    def _resolve_device() -> str | int:
        try:
            import torch

            if torch.cuda.is_available():
                return 0
        except ImportError:
            pass
        return "cpu"

    def _track_kwargs(self) -> dict[str, Any]:
        # Do NOT pass half= here — Ultralytics 8.4+ deprecates it and spams
        # warnings every frame. FP16 is applied once at model load (YoloDetectorWrapper).
        return {
            "persist": True,
            "imgsz": self.imgsz,
            "conf": self.conf,
            "iou": self.iou,
            "max_det": self.max_det,
            "tracker": self.tracker_config,
            "classes": self.allowed_class_indices if self.allowed_class_indices else None,
            "verbose": False,
            "device": self._device,
        }

    def _run_track(self, frame: np.ndarray):
        kwargs = self._track_kwargs()
        with _INFERENCE_LOCK:
            try:
                return self.detector.model.track(source=frame, **kwargs)
            except RuntimeError as exc:
                err = str(exc)
                dtype_mismatch = "same dtype" in err or "Half !=" in err or "c10::Half" in err
                if not self.detector.half or self._half_retry_disabled or not dtype_mismatch:
                    raise
                # Some CUDA/Ultralytics builds fail after model.half() fuse — once-only FP32 fallback.
                self._half_retry_disabled = True
                self.detector.half = False
                self.detector.model.model.float()
                predictor = getattr(self.detector.model, "predictor", None)
                if predictor is not None:
                    predictor.model = None
                kwargs = self._track_kwargs()
                return self.detector.model.track(source=frame, **kwargs)

    def _clear_cache(self) -> None:
        self._cached_tracks = []
        self._cached_raw = []

    def track(self, frame: np.ndarray) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Detect + track one frame.

        Returns:
            tracks: active track dicts (track_id, class_name, confidence, bbox)
            raw_detections: same boxes without requiring track IDs
        """
        self._frame_count += 1
        # Frame 1, 1+N, 1+2N, ... are full detect frames when every_n > 1.
        is_detect_frame = self.detect_every_n == 1 or (self._frame_count % self.detect_every_n == 1)

        if not is_detect_frame:
            # Shallow copies so callers cannot mutate the shared cache lists.
            return list(self._cached_tracks), list(self._cached_raw)

        results = self._run_track(frame)
        if not results:
            self._clear_cache()
            return [], []

        result = results[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            # Critical: clear cache so occupancy/counts do not freeze ghost vehicles.
            self._clear_cache()
            return [], []

        xyxy = boxes.xyxy.cpu().numpy()
        confidences = boxes.conf.cpu().numpy()
        class_indices = boxes.cls.cpu().numpy().astype(int)

        raw_detections: list[dict[str, Any]] = []
        for i in range(len(xyxy)):
            raw_detections.append({
                "bbox": xyxy[i].tolist(),
                "class_name": self.names.get(int(class_indices[i]), "unknown"),
                "confidence": float(confidences[i]),
            })

        track_ids = boxes.id
        if track_ids is None:
            # First frames may lack IDs until ByteTrack warms up — keep raw only.
            self._cached_tracks, self._cached_raw = [], raw_detections
            return [], list(raw_detections)

        track_ids = track_ids.cpu().numpy().astype(int)
        tracks: list[dict[str, Any]] = []
        for i in range(len(track_ids)):
            tracks.append({
                "track_id": int(track_ids[i]),
                "class_name": self.names.get(int(class_indices[i]), "unknown"),
                "confidence": float(confidences[i]),
                "bbox": xyxy[i].tolist(),
            })

        self._cached_tracks, self._cached_raw = tracks, raw_detections
        return list(tracks), list(raw_detections)
