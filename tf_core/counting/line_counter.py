"""Track-based vehicle counting (detect + tracking + lane assignment).

Counting lines / tripwires are intentionally NOT used. A vehicle is counted
once per lane when ByteTrack has a stable lane assignment after min track age.
Direction is inferred from recent motion of the track anchor (bbox bottom-center).
"""

from __future__ import annotations

import logging
from typing import Any

from tf_core.occupancy.lane_state_manager import LaneStateManager

logger = logging.getLogger("TrafficFlow.Counting")

# Kept for import compatibility; counting lines are no longer used at runtime.
def synthesize_counting_line(polygon: list[Any]) -> dict[str, list[float]] | None:  # noqa: ARG001
    """Deprecated — counting lines are not used (detect+track based counting)."""
    return None


class LineCounter:
    """Emit one count event per (track_id, lane_id) from stable lane assignment.

    Event shape matches the historical line-crossing payload so storage,
    Vehicle Counting, and Events pages keep working without tripwires:
      frame, track_id, class_name, lane_id, line_id, direction, confidence
    """

    DIRECTIONS = ("forward", "backward")

    def __init__(self, config: dict):
        self.config = config

        counting_cfg = config.get("counting", {}) or {}
        # Legacy knobs kept for YAML compatibility (ignored for tripwire geometry).
        self.min_cross_distance_px: float = float(
            counting_cfg.get("min_cross_distance_px", 2.0)
        )
        self.count_unstable_lane: bool = bool(
            counting_cfg.get("count_unstable_lane", False)
        )
        # Prefer stable lane only; optional raw-lane fallback for short tracks.
        tracking_cfg = config.get("tracking", {}) or {}
        self.min_track_age_frames: int = int(
            tracking_cfg.get("min_track_age_frames", 3)
        )
        # Motion samples needed to infer forward/backward.
        self._motion_history: int = int(counting_cfg.get("motion_history", 5))

        self._allowed_classes: list[str] = list(
            config.get("detector", {}).get("allowed_classes", [])
        )

        # counts[lane_id][class_name][direction] -> int
        self.counts: dict[str, dict[str, dict[str, int]]] = {}
        for lane in config.get("lanes", []) or []:
            lane_id = str(lane.get("id") or lane.get("lane_id") or "")
            if not lane_id:
                continue
            self.counts[lane_id] = {
                cls: {d: 0 for d in self.DIRECTIONS}
                for cls in self._allowed_classes
            }

        # Dedup: (track_id, lane_id) already counted
        self._counted: set[tuple[int, str]] = set()
        # Recent anchors for motion: track_id -> list[(cx, cy)]
        self._anchors: dict[int, list[tuple[float, float]]] = {}

        n_lanes = len(self.counts)
        logger.info(
            "LineCounter: track-based mode (no counting lines) — %d lane(s)",
            n_lanes,
        )

    def _ensure_class_bucket(self, lane_id: str, class_name: str) -> None:
        bucket = self.counts.setdefault(lane_id, {})
        if class_name not in bucket:
            bucket[class_name] = {d: 0 for d in self.DIRECTIONS}

    def _anchor(self, bbox: list[float]) -> tuple[float, float] | None:
        if not bbox or len(bbox) < 4:
            return None
        xmin, _ymin, xmax, ymax = bbox[:4]
        return ((float(xmin) + float(xmax)) / 2.0, float(ymax))

    def _direction_from_motion(self, track_id: int) -> str:
        """Infer travel direction from anchor motion (image coords).

        Image Y increases downward. Moving toward top of frame (decreasing y)
        is treated as ``forward`` (away from camera on typical traffic cams);
        increasing y is ``backward``.
        """
        hist = self._anchors.get(track_id) or []
        if len(hist) < 2:
            return "forward"
        y0 = hist[0][1]
        y1 = hist[-1][1]
        dy = y1 - y0
        # Small motion → default forward
        if abs(dy) < 2.0:
            x0 = hist[0][0]
            x1 = hist[-1][0]
            # Prefer vertical; if mostly horizontal, still label forward
            if abs(x1 - x0) >= abs(dy):
                return "forward"
            return "forward"
        return "backward" if dy > 0 else "forward"

    def _effective_lane(self, state) -> str | None:
        if state.stable_lane is not None:
            return state.stable_lane
        if not self.count_unstable_lane:
            return None
        if state.raw_history and state.raw_history[-1] not in (None, "unknown"):
            return state.raw_history[-1]
        return None

    def update(
        self,
        current_frame_idx: int,
        state_manager: LaneStateManager,
    ) -> list[dict[str, Any]]:
        """Emit count events for newly lane-assigned tracks this frame."""
        events: list[dict[str, Any]] = []

        for tid, state in state_manager.track_states.items():
            if state.last_seen_frame != current_frame_idx:
                # Drop stale motion history for dead tracks occasionally
                if current_frame_idx - state.last_seen_frame > 60:
                    self._anchors.pop(tid, None)
                continue

            anchor = self._anchor(state.bbox)
            if anchor is not None:
                hist = self._anchors.setdefault(tid, [])
                hist.append(anchor)
                if len(hist) > self._motion_history:
                    del hist[: len(hist) - self._motion_history]

            age = current_frame_idx - state.first_seen_frame + 1
            if age < self.min_track_age_frames:
                continue

            lane_id = self._effective_lane(state)
            if not lane_id or lane_id == "unknown":
                continue

            key = (int(tid), str(lane_id))
            if key in self._counted:
                continue

            self._counted.add(key)
            direction = self._direction_from_motion(int(tid))
            class_name = state.class_name or "unknown"
            self._ensure_class_bucket(str(lane_id), class_name)
            self.counts[str(lane_id)][class_name][direction] += 1

            events.append({
                "frame": current_frame_idx,
                "track_id": tid,
                "class_name": class_name,
                "lane_id": str(lane_id),
                # line_id kept for API/UI compatibility (no geometric line).
                "line_id": f"{lane_id}_track",
                "direction": direction,
                "confidence": float(state.confidence or 0.0),
                "count_mode": "track_lane",
            })

        return events

    def get_counts(self) -> dict[str, dict[str, dict[str, int]]]:
        return self.counts

    def get_lines(self) -> list:
        """No geometric counting lines in track-based mode."""
        return []
