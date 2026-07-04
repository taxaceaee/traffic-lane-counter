"""DetectionCore — pure detection pipeline without storage, visualization, or I/O.

Extracted from TrafficFlowPipeline to enable a standalone detection server
that receives frames and returns JSON results.  The full pipeline (pipeline.py)
wraps this core and adds storage, event logging, file output, and visualization.
"""
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np

from tf_core.counting.line_counter import LineCounter
from tf_core.detection.yolo_detector import ModelRegistry, YoloDetectorWrapper
from tf_core.lanes.lane_assigner import LaneAssigner
from tf_core.occupancy.lane_state_manager import LaneStateManager
from tf_core.occupancy.occupancy_engine import OccupancyEngine
from tf_core.tracking.bytetrack_adapter import YoloByteTrackAdapter


class DetectionCore:
    """Shared detection logic — YOLO + ByteTrack + lane + occupancy + counting.

    Pure detection.  No storage writes, no visualization, no file I/O, no
    video handling.  Call ``process_frame()`` for each BGR frame and receive
    a structured result dict.

    Parameters
    ----------
    config:
        Compiled pipeline config dict (already loaded and validated).
    detector:
        Optional pre-initialised detector.  When ``None`` the detector is
        loaded lazily on the first call to ``start()``.
    """

    def __init__(self, config: dict[str, Any], detector: YoloDetectorWrapper | None = None):
        self.config = config
        self._injected_detector = detector

        # Sub-components — created in start()
        self.detector: YoloDetectorWrapper | None = detector
        self.tracking_adapter: YoloByteTrackAdapter | None = None
        self.lane_assigner: LaneAssigner | None = None
        self.state_manager: LaneStateManager | None = None
        self.occupancy_engine: OccupancyEngine | None = None
        self.line_counter: LineCounter | None = None

        self.frame_idx: int = 0
        self._started: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Lazy-load model weights and initialise all sub-components.

        Safe to call multiple times — only the first call has an effect.
        """
        if self._started:
            return
        self._started = True

        self.lane_assigner = LaneAssigner(self.config)
        self.state_manager = LaneStateManager(self.config)
        self.occupancy_engine = OccupancyEngine(self.config)
        self.line_counter = LineCounter(self.config)

        if self.detector is None:
            half = self.config.get("detector", {}).get("half", False)
            # Use ModelRegistry so cameras sharing the same weights
            # reuse the same GPU-resident model instance
            self.detector = ModelRegistry.get(
                self.config["detector"]["weights"], half=half
            )
        self.tracking_adapter = YoloByteTrackAdapter(self.detector, self.config)

    def reset(self) -> None:
        """Reset internal state so the core can be re-used for a new run."""
        self.frame_idx = 0
        self._started = False
        self.detector = self._injected_detector
        self.tracking_adapter = None
        self.lane_assigner = None
        self.state_manager = None
        self.occupancy_engine = None
        self.line_counter = None

    # ------------------------------------------------------------------
    # Per-frame processing
    # ------------------------------------------------------------------

    def process_frame(
        self,
        frame: np.ndarray,
        frame_idx: int | None = None,
        frame_timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Run the full detection pipeline on a single BGR frame.

        Parameters
        ----------
        frame:
            BGR image as a NumPy array (H, W, 3).
        frame_idx:
            Explicit frame number.  When ``None``, the internal auto-
            increment counter is used (starting at 0).
        frame_timestamp:
            Frame-accurate UTC timestamp.  When ``None``, uses ``datetime.now(timezone.utc)``.

        Returns
        -------
        dict with keys:
            ``frame_idx``: int — the frame number that was used.
            ``frame_timestamp``: datetime — frame-accurate UTC timestamp.
            ``tracks``: list[dict] — active ByteTrack results (track_id,
                class_name, confidence, bbox).
            ``raw_detections``: list[dict] — per-frame detections without
                tracking IDs (class_name, confidence, bbox).
            ``events``: list[dict] — lane-change events (frame, track_id,
                class_name, previous_stable_lane, current_stable_lane).
            ``occupancy``: dict[lane_id → int] — per-lane vehicle count.
            ``crossings``: list[dict] — line-crossing events (frame, track_id,
                class_name, lane_id, line_id, direction, confidence).
            ``frame_tracks``: list[dict] — detailed per-track state for
                consumers (track_id, class_name, confidence, bbox,
                raw_lane, stable_lane, is_counted_in_occupancy).
            ``timing_ms``: dict[str, float] — per-step latencies in ms
                (detect_track, lane_assign, occupancy, counting).
        """
        if frame_idx is None:
            frame_idx = self.frame_idx
            self.frame_idx += 1
        if frame_timestamp is None:
            frame_timestamp = datetime.now(timezone.utc)

        t0 = time.perf_counter()
        tracks, raw_detections = self.tracking_adapter.track(frame)
        t1 = time.perf_counter()

        events = self.state_manager.update(
            frame_idx, tracks, self.lane_assigner
        )
        t2 = time.perf_counter()

        occupancy = self.occupancy_engine.compute_occupancy(
            frame_idx, self.state_manager
        )
        t3 = time.perf_counter()

        crossings = self.line_counter.update(frame_idx, self.state_manager)
        t4 = time.perf_counter()

        frame_tracks = self._build_frame_tracks(frame_idx)

        result = {
            "frame_idx": frame_idx,
            "frame_timestamp": frame_timestamp,
            "tracks": tracks,
            "raw_detections": raw_detections,
            "events": events,
            "occupancy": occupancy,
            "crossings": crossings,
            "frame_tracks": frame_tracks,
            "timing_ms": {
                "detect_track": (t1 - t0) * 1000.0,
                "lane_assign": (t2 - t1) * 1000.0,
                "occupancy": (t3 - t2) * 1000.0,
                "counting": (t4 - t3) * 1000.0,
            },
        }
        return result

    def get_counts(self) -> dict[str, dict[str, dict[str, int]]]:
        """Return cumulative line-crossing tallies per (lane, class, direction)."""
        if self.line_counter is None:
            return {}
        return self.line_counter.get_counts()

    def get_lines(self) -> list:
        """Return the configured CountingLine objects."""
        if self.line_counter is None:
            return []
        return self.line_counter.get_lines()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_frame_tracks(self, frame_idx: int) -> list[dict[str, Any]]:
        """Build the per-frame track records for consumers (file output, etc.)."""
        frame_tracks = []
        min_age = self.state_manager.min_track_age_frames
        for tid, state in self.state_manager.track_states.items():
            if state.last_seen_frame == frame_idx:
                frame_tracks.append({
                    "track_id": tid,
                    "class_name": state.class_name,
                    "confidence": state.confidence,
                    "bbox": state.bbox,
                    "raw_lane": (
                        state.raw_history[-1]
                        if state.raw_history
                        else "unknown"
                    ),
                    "stable_lane": state.stable_lane,
                    "is_counted_in_occupancy": state.is_counted(
                        self.frame_idx, min_age
                    ),
                })
        return frame_tracks
