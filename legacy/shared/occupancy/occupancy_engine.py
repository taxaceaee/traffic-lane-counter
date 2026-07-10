from shared.occupancy.lane_state_manager import LaneStateManager


class OccupancyEngine:
    """Tracks lane occupancy — recomputes every frame.

    O(tracks) scan is typically < 0.1 ms for 20-100 tracks, so delta
    updates aren't worth the complexity.  The occupancy dict is reused
    across frames to avoid per-frame allocation.
    """

    def __init__(self, config: dict):
        self.config = config
        self.lane_ids = [lane["id"] for lane in config.get("lanes", [])]
        tracking_cfg = config.get("tracking", {})
        self.min_track_age_frames = tracking_cfg.get("min_track_age_frames", 3)

        # Reusable occupancy dict — avoids O(lanes) dict creation every frame
        self._occupancy: dict[str, int] = {lid: 0 for lid in self.lane_ids}

    def compute_occupancy(self, current_frame_idx: int, state_manager: LaneStateManager) -> dict[str, int]:
        """Counts active stable tracks per lane.

        Resets counters to zero then scans active tracks — simple,
        correct, and fast enough for <100 tracks.
        """
        # Reset reusable dict (O(lanes) but avoids allocation)
        for lid in self._occupancy:
            self._occupancy[lid] = 0

        for state in state_manager.track_states.values():
            if state.is_counted(current_frame_idx, self.min_track_age_frames):
                lane_id = state.stable_lane
                if lane_id in self._occupancy:
                    self._occupancy[lane_id] += 1

        return self._occupancy

    def update_lanes(self, lanes_data: list[dict]) -> None:
        """Hot-swap lane IDs — rebuilds the occupancy dict for new lane set.

        Existing tracks with stale lane IDs will be silently excluded from
        occupancy until their stable_lane settles to a valid new lane ID.
        """
        new_ids = []
        for lane in lanes_data:
            lid = lane["id"] if "id" in lane else lane.get("lane_id", "")
            new_ids.append(lid)

        self.lane_ids = new_ids
        self._occupancy = {lid: 0 for lid in self.lane_ids}

    def reset(self) -> None:
        """Reset occupancy counters — call when restarting a stream."""
        for lid in self._occupancy:
            self._occupancy[lid] = 0

