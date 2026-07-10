
import numpy as np


class Lane:
    """Represents a manually configured lane defined by a polygon.

    Precomputes axis-aligned bounding box (AABB) for quick-reject in
    LaneAssigner — avoids expensive cv2.pointPolygonTest calls for
    points that are clearly outside the polygon.
    """
    __slots__ = ("id", "points", "polygon", "xmin", "xmax", "ymin", "ymax")

    def __init__(self, lane_id: str, points: list[list[float]]):
        self.id = lane_id
        self.points = points
        # Reshape to (N, 1, 2) of type float32 as expected by cv2.pointPolygonTest
        self.polygon = np.array(points, dtype=np.float32).reshape((-1, 1, 2))

        # Precompute AABB for quick-reject
        pts = np.array(points, dtype=np.float32)
        self.xmin = float(pts[:, 0].min())
        self.xmax = float(pts[:, 0].max())
        self.ymin = float(pts[:, 1].min())
        self.ymax = float(pts[:, 1].max())

    def contains_point_quick(self, x: float, y: float) -> bool:
        """AABB quick-reject: returns False if point is clearly outside."""
        return self.xmin <= x <= self.xmax and self.ymin <= y <= self.ymax

    def update_polygon(self, points: list[list[float]]) -> None:
        """In-place update polygon + AABB without re-creating the Lane object."""
        self.points = points
        self.polygon = np.array(points, dtype=np.float32).reshape((-1, 1, 2))
        pts = np.array(points, dtype=np.float32)
        self.xmin = float(pts[:, 0].min())
        self.xmax = float(pts[:, 0].max())
        self.ymin = float(pts[:, 1].min())
        self.ymax = float(pts[:, 1].max())
