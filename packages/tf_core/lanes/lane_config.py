
import numpy as np


class Lane:
    """Represents a manually configured lane defined by a polygon."""
    def __init__(self, lane_id: str, points: list[list[float]]):
        self.id = lane_id
        self.points = points
        # Reshape to (N, 1, 2) of type int32 as expected by cv2.pointPolygonTest
        self.polygon = np.array(points, dtype=np.float32).reshape((-1, 1, 2))
