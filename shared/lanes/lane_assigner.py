
import cv2

from shared.lanes.lane_config import Lane


class LaneAssigner:
    """Assigns tracked vehicle bounding boxes to configured lanes.

    Uses cv2.pointPolygonTest on the bottom-center point of the bounding box.
    Quick-reject via precomputed AABB before calling pointPolygonTest.
    """
    def __init__(self, config: dict):
        self.config = config
        self.lane_assign_config = config.get("lane_assignment", {})
        self.boundary_mode = self.lane_assign_config.get("boundary_mode", "inside_or_on_edge")

        self.lanes = []
        for lane_data in config.get("lanes", []):
            self.lanes.append(Lane(lane_data["id"], lane_data["points"]))

    def assign_lane(self, bbox: list[float]) -> str:
        """Determines which lane polygon contains the bottom-center of the bbox.

        Args:
            bbox: A bounding box list [xmin, ymin, xmax, ymax].

        Returns:
            The lane ID string (e.g. 'lane_1'), or 'unknown' if outside all lanes.
        """
        xmin, _ymin, xmax, ymax = bbox
        x_center = (xmin + xmax) / 2.0
        y_bottom = ymax
        pt = (x_center, y_bottom)

        # Search lanes in configuration order, with AABB quick-reject
        for lane in self.lanes:
            # Quick-reject: skip if point is clearly outside lane bounding box
            if not lane.contains_point_quick(x_center, y_bottom):
                continue

            # dist is > 0 (inside), = 0 (on edge), or < 0 (outside)
            dist = cv2.pointPolygonTest(lane.polygon, pt, measureDist=False)
            if self.boundary_mode == "inside_or_on_edge":
                if dist >= 0:
                    return lane.id
            elif self.boundary_mode == "inside_only" and dist > 0:
                return lane.id

        return "unknown"

    def update_lanes(self, lanes_data: list[dict]) -> list[str]:
        """Hot-swap lane polygons in-place without re-creating LaneAssigner.

        Strategies:
        - Existing lane IDs: update polygon + AABB in-place
        - New lane IDs: append new Lane
        - Removed lane IDs: keep but remove them so assign_lane still works correctly

        Returns:
            List of removed lane IDs (for cleanup in other components).
        """
        existing_ids = {l.id for l in self.lanes}
        new_ids = {ld["id"] for ld in lanes_data}
        removed_ids = existing_ids - new_ids

        # Build lookup for fast access
        lane_map = {l.id: l for l in self.lanes}

        new_lanes = []
        for ld in lanes_data:
            lid = ld["id"]
            if lid in lane_map:
                # Update in-place
                lane_map[lid].update_polygon(ld["points"])
                new_lanes.append(lane_map[lid])
            else:
                # New lane
                new_lanes.append(Lane(lid, ld["points"]))

        self.lanes = new_lanes
        self.config["lanes"] = lanes_data
        return list(removed_ids)
