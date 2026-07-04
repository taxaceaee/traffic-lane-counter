import logging
from typing import Any

from shared.counting.counting_line import CountingLine
from shared.occupancy.lane_state_manager import LaneStateManager

logger = logging.getLogger("TrafficFlow.Counting")


class LineCounter:
    """Per-frame line-crossing counter.

    For each visible track, computes the signed distance to each configured
    counting line, detects sign flips (with a hysteresis band to ignore
    sub-pixel jitter), and emits crossing events tagged with lane and
    direction. Cumulative counts are tallied per (lane, class, direction).
    """

    DIRECTIONS = ("forward", "backward")

    def __init__(self, config: dict):
        self.config = config

        counting_cfg = config.get("counting", {}) or {}
        self.min_cross_distance_px: float = float(
            counting_cfg.get("min_cross_distance_px", 2.0)
        )
        self.count_unstable_lane: bool = bool(
            counting_cfg.get("count_unstable_lane", True)
        )

        # Track age gate (reuse the existing tracking config knob)
        tracking_cfg = config.get("tracking", {}) or {}
        self.min_track_age_frames: int = int(
            tracking_cfg.get("min_track_age_frames", 3)
        )

        self.lines: list[CountingLine] = []
        for lane in config.get("lanes", []):
            cl = lane.get("counting_line")
            if not cl:
                continue
            lane_id = lane["id"]
            line = CountingLine(
                line_id=f"{lane_id}_count",
                lane_id=lane_id,
                start=cl["start"],
                end=cl["end"],
                direction_ref=cl["direction_ref"],
            )
            self.lines.append(line)

        lanes_with_count = {ln.lane_id for ln in self.lines}
        lanes_without = [
            lane["id"]
            for lane in config.get("lanes", [])
            if lane["id"] not in lanes_with_count
        ]
        if lanes_without:
            logger.info(
                "LineCounter: lanes without counting_line (no counts emitted): %s",
                lanes_without,
            )

        self._allowed_classes: list[str] = list(
            config.get("detector", {}).get("allowed_classes", [])
        )

        # counts[lane_id][class_name][direction] -> int
        self.counts: dict[str, dict[str, dict[str, int]]] = {}
        for line in self.lines:
            self.counts[line.lane_id] = {
                cls: {d: 0 for d in self.DIRECTIONS}
                for cls in self._allowed_classes
            }

    def _effective_lane(self, state) -> str | None:
        if state.stable_lane is not None:
            return state.stable_lane
        if not self.count_unstable_lane:
            return None
        if state.raw_history and state.raw_history[-1] != "unknown":
            return state.raw_history[-1]
        return None

    def _ensure_class_bucket(self, lane_id: str, class_name: str) -> None:
        # Allow classes not pre-declared in allowed_classes (e.g. if config drifts).
        bucket = self.counts.setdefault(lane_id, {})
        if class_name not in bucket:
            bucket[class_name] = {d: 0 for d in self.DIRECTIONS}

    def update(
        self,
        current_frame_idx: int,
        state_manager: LaneStateManager,
    ) -> list[dict[str, Any]]:
        """Updates per-track side state and emits crossing events.

        Returns crossing events for the current frame.
        """
        events: list[dict[str, Any]] = []
        if not self.lines:
            return events

        eps = self.min_cross_distance_px

        for tid, state in state_manager.track_states.items():
            # Only tracks visible this frame
            if state.last_seen_frame != current_frame_idx:
                continue

            bbox = state.bbox
            if not bbox or len(bbox) < 4:
                continue
            xmin, _ymin, xmax, ymax = bbox
            anchor = ((xmin + xmax) / 2.0, ymax)

            age = current_frame_idx - state.first_seen_frame + 1
            age_ok = age >= self.min_track_age_frames

            for line in self.lines:
                # Off-segment: anchor's perpendicular projection falls outside [start, end].
                # In that case the infinite-line sign is meaningless and would cause cross-lane
                # false positives. Also clear any stale last_side so re-entering the segment
                # acts like a first observation.
                if not line.projects_within_segment(anchor):
                    state_manager.clear_last_side(tid, line.line_id)
                    continue

                sd = line.signed_distance(anchor)

                # Sub-epsilon: leave last_side alone, no event.
                if abs(sd) < eps:
                    continue

                new_side = 1 if sd > 0 else -1
                prev_side = state_manager.get_last_side(tid, line.line_id)

                if prev_side is None:
                    # First confident observation for this (track, line). Just record.
                    state_manager.update_last_side(tid, line.line_id, new_side)
                    continue

                if new_side == prev_side:
                    # Same side, no flip.
                    continue

                # Real flip past hysteresis.
                state_manager.update_last_side(tid, line.line_id, new_side)

                if not age_ok:
                    continue

                eff_lane = self._effective_lane(state)
                if eff_lane != line.lane_id:
                    continue

                direction = line.direction_from_flip(prev_side, new_side)
                if direction is None:
                    continue

                self._ensure_class_bucket(line.lane_id, state.class_name)
                self.counts[line.lane_id][state.class_name][direction] += 1

                events.append({
                    "frame": current_frame_idx,
                    "track_id": tid,
                    "class_name": state.class_name,
                    "lane_id": line.lane_id,
                    "line_id": line.line_id,
                    "direction": direction,
                    "confidence": state.confidence,
                })

        return events

    def get_counts(self) -> dict[str, dict[str, dict[str, int]]]:
        return self.counts

    def get_lines(self) -> list[CountingLine]:
        return list(self.lines)
