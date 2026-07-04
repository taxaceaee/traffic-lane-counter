from typing import Any

from shared.lanes.lane_assigner import LaneAssigner


class TrackState:
    """Represents the persistent state of a single tracked object."""
    def __init__(self, track_id: int, class_name: str, first_seen_frame: int):
        self.track_id = track_id
        self.class_name = class_name
        self.first_seen_frame = first_seen_frame

        self.last_seen_frame = first_seen_frame
        self.bbox: list[float] = []
        self.confidence: float = 0.0

        # History of raw lane classifications (max length: history_window)
        self.raw_history: list[str] = []

        # Smoothed state
        self.stable_lane: str | None = None
        self.consecutive_unknown_count = 0

        # Per counting-line side memory (line_id -> +1 / -1).
        # Sub-epsilon and zero are never stored. Instance attr (not class) on purpose.
        self.last_side: dict[str, int] = {}

    def is_counted(self, current_frame_idx: int, min_age: int) -> bool:
        """Determines if the track satisfies minimum age and has a valid stable lane."""
        age = current_frame_idx - self.first_seen_frame + 1
        return age >= min_age and self.stable_lane is not None

class LaneStateManager:
    """Manages track state lifecycles, lane assignment smoothing, and timeout logic."""
    def __init__(self, config: dict):
        self.config = config

        # Tracking config
        tracking_cfg = config.get("tracking", {})
        self.active_track_timeout_frames = tracking_cfg.get("active_track_timeout_frames", 10)
        self.min_track_age_frames = tracking_cfg.get("min_track_age_frames", 3)

        # Smoothing config
        smoothing_cfg = config.get("smoothing", {})
        self.history_window = smoothing_cfg.get("history_window", 10)
        self.min_consecutive_for_change = smoothing_cfg.get("min_consecutive_for_change", 5)

        # Lane assignment config (timeouts/policies)
        lane_assign_cfg = config.get("lane_assignment", {})
        self.unknown_timeout_frames = lane_assign_cfg.get("unknown_timeout_frames", 15)

        # Active track states mapped by track_id
        self.track_states: dict[int, TrackState] = {}

        # Store lane changes triggered in the current frame update
        self.current_frame_events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Counting-line side state API (accessed by LineCounter through these
    # methods instead of mutating TrackState.last_side directly).
    # ------------------------------------------------------------------

    def get_last_side(self, track_id: int, line_id: str) -> int | None:
        state = self.track_states.get(track_id)
        if state is None:
            return None
        return state.last_side.get(line_id)

    def update_last_side(self, track_id: int, line_id: str, side: int) -> None:
        state = self.track_states.get(track_id)
        if state is not None:
            state.last_side[line_id] = side

    def clear_last_side(self, track_id: int, line_id: str) -> None:
        state = self.track_states.get(track_id)
        if state is not None:
            state.last_side.pop(line_id, None)

    def update(self, current_frame_idx: int, detected_tracks: list[dict[str, Any]], lane_assigner: LaneAssigner) -> list[dict[str, Any]]:
        """Updates the state of all tracked objects and identifies lane change events.

        Args:
            current_frame_idx: Index of the current frame.
            detected_tracks: Active tracks from the detector/tracker.
            lane_assigner: Component to get the raw lane for a track box.

        Returns:
            A list of lane change event dictionaries.
        """
        self.current_frame_events = []
        seen_track_ids = set()

        # 1. Update states of tracks present in the current frame
        for track in detected_tracks:
            tid = track["track_id"]
            cls_name = track["class_name"]
            conf = track["confidence"]
            bbox = track["bbox"]

            seen_track_ids.add(tid)

            # Determine raw lane assignment
            raw_lane = lane_assigner.assign_lane(bbox)

            if tid not in self.track_states:
                # New track detected
                self.track_states[tid] = TrackState(tid, cls_name, current_frame_idx)

            state = self.track_states[tid]
            state.last_seen_frame = current_frame_idx
            state.bbox = bbox
            state.confidence = conf

            # Update raw history
            state.raw_history.append(raw_lane)
            if len(state.raw_history) > self.history_window:
                state.raw_history.pop(0)

            previous_stable = state.stable_lane

            # Smoothing logic
            if raw_lane == "unknown":
                state.consecutive_unknown_count += 1
                if state.consecutive_unknown_count > self.unknown_timeout_frames:
                    state.stable_lane = None
            else:
                state.consecutive_unknown_count = 0

                # Check 1: Consecutive frames override (fast change)
                C = self.min_consecutive_for_change
                last_C = state.raw_history[-C:] if len(state.raw_history) >= C else []
                if len(last_C) == C and all(x == last_C[0] and x != "unknown" for x in last_C):
                    state.stable_lane = last_C[0]
                else:
                    # Check 2: Majority vote of the sliding window history
                    valid_counts = {}
                    for x in state.raw_history:
                        if x != "unknown":
                            valid_counts[x] = valid_counts.get(x, 0) + 1

                    if valid_counts:
                        max_count = max(valid_counts.values())
                        modes = [lane_id for lane_id, count in valid_counts.items() if count == max_count]
                        # Set to mode only if there is a unique majority mode
                        if len(modes) == 1:
                            state.stable_lane = modes[0]

            # Check for confirmed stable lane changes (from one valid lane to another)
            if (previous_stable is not None
                and state.stable_lane is not None
                and previous_stable != state.stable_lane):

                event = {
                    "frame": current_frame_idx,
                    "track_id": tid,
                    "class_name": cls_name,
                    "previous_stable_lane": previous_stable,
                    "current_stable_lane": state.stable_lane
                }
                self.current_frame_events.append(event)

        # 2. Cleanup stale track states (timeout check)
        dead_track_ids = []
        for tid, state in self.track_states.items():
            if tid not in seen_track_ids:
                frames_missing = current_frame_idx - state.last_seen_frame
                if frames_missing > self.active_track_timeout_frames:
                    dead_track_ids.append(tid)

        for tid in dead_track_ids:
            del self.track_states[tid]

        return self.current_frame_events
