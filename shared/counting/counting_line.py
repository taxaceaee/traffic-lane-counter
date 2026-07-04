

class CountingLine:
    """A virtual tripwire associated with a lane.

    Given a line segment start->end and a direction_ref point that marks the
    "approaching" side, computes the signed cross-product distance from any
    anchor point to the line. The sign tells you which side of the line the
    point is on; a sign flip between frames is a crossing.
    """

    def __init__(
        self,
        line_id: str,
        lane_id: str,
        start: list[float],
        end: list[float],
        direction_ref: list[float],
    ):
        self.line_id = line_id
        self.lane_id = lane_id
        self.start = (float(start[0]), float(start[1]))
        self.end = (float(end[0]), float(end[1]))
        self.direction_ref = (float(direction_ref[0]), float(direction_ref[1]))

        # Precompute the direction vector of the line
        self._dx = self.end[0] - self.start[0]
        self._dy = self.end[1] - self.start[1]
        self._length_sq = self._dx * self._dx + self._dy * self._dy

        # Precompute the "approaching" side sign from direction_ref
        ref_sd = self.signed_distance(self.direction_ref)
        if ref_sd > 0:
            self.in_sign = 1
        elif ref_sd < 0:
            self.in_sign = -1
        else:
            # Should be caught by config validation, but defend in depth.
            raise ValueError(
                f"Counting line {line_id}: direction_ref {direction_ref} is collinear "
                f"with the line {start}->{end}; direction would be undefined."
            )

    def signed_distance(self, point: tuple[float, float]) -> float:
        """Signed cross-product distance from `point` to the line.

        Positive on one side, negative on the other, zero exactly on the line.
        Magnitude is proportional to the perpendicular pixel distance scaled
        by the line length, which is fine for hysteresis comparisons when the
        line length is constant.

        Note: this treats the line as INFINITE. Use `projects_within_segment`
        to gate crossings to the bounded segment.
        """
        px, py = point[0], point[1]
        return self._dx * (py - self.start[1]) - self._dy * (px - self.start[0])

    def projects_within_segment(self, point: tuple[float, float], tolerance: float = 0.0) -> bool:
        """Returns True if the perpendicular projection of `point` onto the
        line falls within the [start, end] segment (with optional tolerance).

        Required to prevent vehicles far off to the side of a short line
        from triggering false crossings — `signed_distance` alone treats the
        line as infinite.
        """
        if self._length_sq <= 0:
            return False
        px, py = point[0], point[1]
        t = ((px - self.start[0]) * self._dx + (py - self.start[1]) * self._dy) / self._length_sq
        return -tolerance <= t <= 1.0 + tolerance

    def direction_from_flip(self, prev_side: int, new_side: int) -> str | None:
        """Returns 'forward' or 'backward' for a valid flip, else None."""
        if prev_side == self.in_sign and new_side == -self.in_sign:
            return "forward"
        if prev_side == -self.in_sign and new_side == self.in_sign:
            return "backward"
        return None
