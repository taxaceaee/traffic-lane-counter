"""CropROI — zone-aware frame cropping to reduce inference area.

Computes the union bounding box of all *detection zone* polygons, crops
the frame to that region, and transforms coordinate systems so that
DetectionCore runs only on the relevant area.

If no zones are provided, falls back to the union of lane polygons
(original behaviour). If neither zones nor lanes exist, uses full frame.

Benefits
--------
- Smaller inference area (typically 40-60 % of full frame) → proportional FPS gain
- Excludes irrelevant regions (sidewalks, sky) → fewer false positives
- No accuracy loss — user-defined zones define the relevant region

Usage
-----
    roi = CropROI(config["lanes"], config["frame_size"], padding=50,
                  zone_polygons=config.get("zone_polygons"))
    frame_roi = roi.crop(frame)
    crop_config = roi.transform_config(config)   # for DetectionCore
    original_bbox = roi.to_original(detection_bbox)  # map back
"""
from __future__ import annotations

import copy
from typing import Any

import numpy as np


class CropROI:
    """Compute and apply a crop region derived from detection zones or lane polygons.

    Parameters
    ----------
    lanes_config:
        List of lane dicts from the compiled config. Each lane must have
        a ``points`` key: ``[[x1,y1],[x2,y2],...]``.
        Only used when ``zone_polygons`` is not provided.
    frame_size:
        Dict with ``width`` and ``height`` keys (original frame dimensions).
    padding:
        Extra pixels added on each side of the crop bounding box.
        Default 50 — provides safety margin for vehicles near edges.
    zone_polygons:
        Optional list of zone polygon point lists (each is ``[[x,y],...]``).
        When provided, the crop region is the union bounding box of these
        zones instead of the lane polygon union.
    """

    def __init__(
        self,
        lanes_config: list[dict[str, Any]],
        frame_size: dict[str, int],
        padding: int = 50,
        zone_polygons: list[list[list[float]]] | None = None,
    ):
        self.frame_width = frame_size["width"]
        self.frame_height = frame_size["height"]
        self.padding = padding

        # Determine source polygons: zones take priority, fall back to lanes
        source_polygons: list[list[list[float]]] | None = zone_polygons
        if not source_polygons:
            # Fallback: extract polygons from lanes config
            source_polygons = [
                lane.get("points", [])
                for lane in lanes_config
                if len(lane.get("points", [])) >= 3
            ]

        # Compute union bounding box from source polygons
        x_min = float("inf")
        y_min = float("inf")
        x_max = float("-inf")
        y_max = float("-inf")

        for poly in source_polygons:
            for pt in poly:
                x, y = float(pt[0]), float(pt[1])
                x_min = min(x_min, x)
                y_min = min(y_min, y)
                x_max = max(x_max, x)
                y_max = max(y_max, y)

        if x_min == float("inf"):
            # No polygons — fall back to full frame
            self.x1 = 0
            self.y1 = 0
            self.x2 = self.frame_width
            self.y2 = self.frame_height
        else:
            # Add padding, clamp to frame boundaries
            self.x1 = max(0, int(x_min) - padding)
            self.y1 = max(0, int(y_min) - padding)
            self.x2 = min(self.frame_width, int(x_max) + padding)
            self.y2 = min(self.frame_height, int(y_max) + padding)

        self.crop_width = self.x2 - self.x1
        self.crop_height = self.y2 - self.y1
        self.offset_x = self.x1
        self.offset_y = self.y1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crop(self, frame: np.ndarray) -> np.ndarray:
        """Crop frame to the lane ROI.

        Returns the cropped region. The returned array shares memory with
        the input (no copy) for performance.
        """
        return frame[self.y1:self.y2, self.x1:self.x2]

    def transform_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Return a deep copy of ``config`` with all coordinates offset to crop space.

        Offsets:
            - lane polygon ``points``
            - counting line ``start``, ``end``, ``direction_ref``
            - ``frame_size`` (updated to crop dimensions)
            - ``coordinate_space`` → ``"crop_space"``

        DetectionCore using this config will produce coordinates relative to
        the cropped frame.
        """
        cfg = copy.deepcopy(config)

        # Update frame_size to crop dimensions
        cfg["frame_size"] = {"width": self.crop_width, "height": self.crop_height}
        cfg["coordinate_space"] = "crop_space"

        # Offset lane polygon points
        for lane in cfg.get("lanes", []):
            points = lane.get("points", [])
            lane["points"] = [
                [pt[0] - self.offset_x, pt[1] - self.offset_y]
                for pt in points
            ]

            # Offset counting line
            cl = lane.get("counting_line")
            if cl:
                if "start" in cl:
                    cl["start"] = [cl["start"][0] - self.offset_x, cl["start"][1] - self.offset_y]
                if "end" in cl:
                    cl["end"] = [cl["end"][0] - self.offset_x, cl["end"][1] - self.offset_y]
                if "direction_ref" in cl:
                    cl["direction_ref"] = [
                        cl["direction_ref"][0] - self.offset_x,
                        cl["direction_ref"][1] - self.offset_y,
                    ]

        return cfg

    def suggested_imgsz(self, stride: int = 32) -> int:
        """Return optimal imgsz for this crop region.

        Uses the longer edge of the crop, rounded up to the nearest
        ``stride`` multiple (default 32 — YOLO stride).  This ensures
        the model runs at the native resolution of the ROI without
        wasteful up-scaling of small crops or lossy down-scaling of
        large crops.
        """
        longest = max(self.crop_width, self.crop_height)
        return ((longest + stride - 1) // stride) * stride

    def to_original(
        self,
        bbox: list[float],
        frame_width: int | None = None,
        frame_height: int | None = None,
    ) -> list[float]:
        """Map a bounding box from crop space back to original frame space.

        ``bbox``: ``[xmin, ymin, xmax, ymax]`` in crop coordinates.

        When ``frame_width`` / ``frame_height`` are provided the result
        is clamped to ``[0, frame_width]`` / ``[0, frame_height]`` so
        boxes never extend outside the visible area.  A bbox that falls
        entirely outside the frame is returned as a zero-area box at the
        nearest edge.
        """
        x1 = bbox[0] + self.offset_x
        y1 = bbox[1] + self.offset_y
        x2 = bbox[2] + self.offset_x
        y2 = bbox[3] + self.offset_y

        if frame_width is not None:
            x1 = max(0.0, min(x1, float(frame_width)))
            x2 = max(0.0, min(x2, float(frame_width)))
        if frame_height is not None:
            y1 = max(0.0, min(y1, float(frame_height)))
            y2 = max(0.0, min(y2, float(frame_height)))

        # Degenerate case: both edges clamped to same boundary → collapse
        # to a 1x1 box at that edge so downstream code (crop extraction,
        # visualizer) doesn't get a zero-area bbox.
        if frame_width is not None and x2 <= x1:
            if x1 >= float(frame_width):
                x1 = float(frame_width) - 1.0
            x2 = x1 + 1.0
        if frame_height is not None and y2 <= y1:
            if y1 >= float(frame_height):
                y1 = float(frame_height) - 1.0
            y2 = y1 + 1.0

        return [x1, y1, x2, y2]

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def area_ratio(self) -> float:
        """Fraction of the original frame that the crop covers (0.0 — 1.0)."""
        total = self.frame_width * self.frame_height
        if total == 0:
            return 1.0
        return (self.crop_width * self.crop_height) / total

    def __repr__(self) -> str:
        return (
            f"CropROI(offset=({self.offset_x},{self.offset_y}), "
            f"size={self.crop_width}x{self.crop_height}, "
            f"padding={self.padding}, area_ratio={self.area_ratio:.2f})"
        )
