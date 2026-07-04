
import cv2

from shared.lanes.lane_config import Lane


class LaneAssigner:
    """Assigns tracked vehicle bounding boxes to configured lanes.

    Uses cv2.pointPolygonTest on the bottom-center point of the bounding box.
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

        # Search lanes in configuration order
        for lane in self.lanes:
            # dist is > 0 (inside), = 0 (on edge), or < 0 (outside)
            dist = cv2.pointPolygonTest(lane.polygon, pt, measureDist=False)
            if self.boundary_mode == "inside_or_on_edge":
                if dist >= 0:
                    return lane.id
            elif self.boundary_mode == "inside_only" and dist > 0:
                return lane.id

        return "unknown"
