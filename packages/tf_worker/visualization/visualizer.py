from typing import Any

import cv2
import numpy as np

from tf_common.viz_colors import build_lane_color_map, color_for_lane, color_for_track
from tf_core.lanes.lane_config import Lane
from tf_core.occupancy.lane_state_manager import LaneStateManager

# Short class labels for the compact COUNTS panel
_CLASS_ABBR = {
    "bicycle": "bik",
    "car": "car",
    "motorcycle": "mot",
    "bus": "bus",
    "truck": "trk",
}


class Visualizer:
    """Renders lane boundaries, bounding boxes, labels, occupancy, and counts panels."""

    def __init__(self, config: dict):
        self.config = config
        self.lanes = [Lane(ln["id"], ln["points"]) for ln in config.get("lanes", [])]
        self._lane_colors = build_lane_color_map([ln.id for ln in self.lanes])

    def _draw_semi_transparent_rect(
        self,
        overlay: np.ndarray,
        pt1: tuple[int, int],
        pt2: tuple[int, int],
        color: tuple[int, int, int],
    ) -> None:
        """Fill a rectangle on a pre-created overlay (blended later via ``_apply_overlay``)."""
        cv2.rectangle(overlay, pt1, pt2, color, -1)

    def _apply_overlay(self, canvas: np.ndarray, overlay: np.ndarray, alpha: float):
        cv2.addWeighted(overlay, alpha, canvas, 1.0 - alpha, 0, canvas)

    def _draw_counts_panel(self, canvas: np.ndarray, line_counter, anchor_y: int):
        """Renders a COUNTS panel below the OCCUPANCY panel."""
        counts = line_counter.get_counts()
        if not counts:
            return

        # Build display lines: one row per lane, "  cls fwd/bwd  ..." truncated to fit.
        lane_ids = sorted(counts.keys())
        panel_w = 360
        line_h = 22
        header_h = 30
        panel_h = header_h + len(lane_ids) * line_h + 10

        x, y = 15, anchor_y
        self._draw_semi_transparent_rect(canvas, (x, y), (x + panel_w, y + panel_h), (20, 20, 20))
        cv2.rectangle(canvas, (x, y), (x + panel_w, y + panel_h), (80, 80, 80), 1)

        cv2.putText(canvas, "COUNTS (fwd / bwd)", (x + 10, y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
        cv2.line(canvas, (x + 10, y + 26), (x + panel_w - 10, y + 26), (80, 80, 80), 1)

        row_y = y + header_h + 12
        for lane_id in lane_ids:
            class_counts = counts[lane_id]
            cv2.putText(canvas, lane_id, (x + 10, row_y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 220, 220), 1)
            col_x = x + 78
            # Render in a stable order: bicycle, car, motorcycle, bus, truck (anything else trailing).
            stable_order = ["bicycle", "car", "motorcycle", "bus", "truck"]
            extra = [c for c in class_counts if c not in stable_order]
            for cls in stable_order + extra:
                if cls not in class_counts:
                    continue
                fwd = class_counts[cls].get("forward", 0)
                bwd = class_counts[cls].get("backward", 0)
                label = f"{_CLASS_ABBR.get(cls, cls[:3])} {fwd}/{bwd}"
                cv2.putText(canvas, label, (col_x, row_y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 127), 1)
                col_x += 60
            row_y += line_h

    def draw(
        self,
        frame: np.ndarray,
        current_frame_idx: int,
        state_manager: LaneStateManager,
        occupancy: dict[str, int],
        line_counter=None,
        crossings_this_frame: list[dict[str, Any]] | None = None,
    ) -> np.ndarray:
        """Annotates a frame with the lane lines, bounding boxes, and statistics table.

        Args:
            frame: Input BGR image frame.
            current_frame_idx: Current frame index.
            state_manager: The lane state manager containing current active tracks.
            occupancy: Current occupancy counts per lane.

        Returns:
            The annotated BGR frame.
        """
        # Single working copy + single overlay for all semi-transparent ops
        canvas = frame.copy()
        overlay = np.full_like(canvas, 0, dtype=np.uint8)

        # Counting is track+lane based — tripwire lines are not drawn.
        _ = crossings_this_frame  # kept for API compatibility

        # 1. Draw Lane Polygons (same color family as vehicle boxes in that lane)
        for lane in self.lanes:
            lane_color = color_for_lane(lane.id, self._lane_colors)
            pts = np.array(lane.polygon, dtype=np.int32).reshape((-1, 1, 2))
            cv2.polylines(canvas, [pts], isClosed=True, color=lane_color, thickness=2)
            first_pt = lane.points[0]
            label_pos = (int(first_pt[0]) + 5, int(first_pt[1]) - 5)
            cv2.putText(canvas, lane.id, label_pos, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.putText(canvas, lane.id, label_pos, cv2.FONT_HERSHEY_SIMPLEX, 0.6, lane_color, 1)

        # 2. Draw Active Bounding Boxes & Labels (same color per lane)
        for tid, state in state_manager.track_states.items():
            if state.last_seen_frame != current_frame_idx:
                continue
            if not state.bbox or len(state.bbox) < 4:
                continue

            xmin, ymin, xmax, ymax = state.bbox
            x1, y1, x2, y2 = int(xmin), int(ymin), int(xmax), int(ymax)

            raw_lane = (
                state.raw_history[-1]
                if getattr(state, "raw_history", None)
                else None
            )
            box_color = color_for_track(
                self._lane_colors,
                stable_lane=state.stable_lane,
                raw_lane=raw_lane,
            )

            cv2.rectangle(canvas, (x1, y1), (x2, y2), box_color, 2)

            cx = int((xmin + xmax) / 2)
            cy = int(ymax)
            cv2.circle(canvas, (cx, cy), 5, box_color, -1)

            stable_lbl = state.stable_lane or raw_lane or "none"
            label_text = f"#{tid} {state.class_name} ({stable_lbl})"

            (tw, th), _ = cv2.getTextSize(label_text, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)
            ty1 = max(y1 - th - 6, 0)
            ty2 = y1
            tx1 = x1
            tx2 = min(x1 + tw + 6, canvas.shape[1])

            self._draw_semi_transparent_rect(overlay, (tx1, ty1), (tx2, ty2), (30, 30, 30))
            cv2.putText(canvas, label_text, (tx1 + 3, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, box_color, 1)

        # 3. Draw On-Screen Occupancy Table
        panel_w = 200
        panel_h = 40 + (len(occupancy) * 25)
        self._draw_semi_transparent_rect(overlay, (15, 15), (15 + panel_w, 15 + panel_h), (20, 20, 20))
        cv2.rectangle(canvas, (15, 15), (15 + panel_w, 15 + panel_h), (80, 80, 80), 1)

        cv2.putText(canvas, "LANE OCCUPANCY", (25, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
        cv2.line(canvas, (25, 42), (15 + panel_w - 10, 42), (80, 80, 80), 1)

        y_pos = 62
        for lane_id in sorted(occupancy.keys()):
            count = occupancy[lane_id]
            lane_color = color_for_lane(lane_id, self._lane_colors)
            cv2.putText(canvas, f"{lane_id}:", (25, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.45, lane_color, 1)
            cv2.putText(canvas, str(count), (15 + panel_w - 40, y_pos), cv2.FONT_HERSHEY_SIMPLEX, 0.5, lane_color, 2)
            y_pos += 25

        # 4. Apply overlay once and draw COUNTS panel
        self._apply_overlay(canvas, overlay, alpha=0.5)
        if line_counter is not None:
            counts_panel_y = 15 + panel_h + 12
            self._draw_counts_panel(canvas, line_counter, counts_panel_y)

        return canvas
