from shared.occupancy.lane_state_manager import LaneStateManager


class OccupancyEngine:
    """Recomputes lane occupancy from scratch in every frame based on active stable tracks."""
    def __init__(self, config: dict):
        self.config = config
        self.lane_ids = [lane["id"] for lane in config.get("lanes", [])]
        tracking_cfg = config.get("tracking", {})
        self.min_track_age_frames = tracking_cfg.get("min_track_age_frames", 3)

    def compute_occupancy(self, current_frame_idx: int, state_manager: LaneStateManager) -> dict[str, int]:
        """Counts the active stable tracks in each lane.

        Args:
            current_frame_idx: Index of the current frame.
            state_manager: The state manager storing current active track states.

        Returns:
            A dictionary mapping lane IDs to current occupancy counts (e.g. {'lane_1': 2}).
        """
        # Recompute from scratch
        occupancy = {lane_id: 0 for lane_id in self.lane_ids}

        for state in state_manager.track_states.values():
            if state.is_counted(current_frame_idx, self.min_track_age_frames):
                lane_id = state.stable_lane
                if lane_id in occupancy:
                    occupancy[lane_id] += 1

        return occupancy
