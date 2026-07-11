"""Shared BGR color palette for lane-aware visualization.

Vehicles in the same lane share one bounding-box color so lanes are easy
to distinguish on live streams and annotated videos. Palette aligns with
the frontend lane editor strokes in ``frontend/js/pages/lanes.js``.
"""

from __future__ import annotations

# Distinct OpenCV BGR colors (hex strokes → BGR):
# #818cf8, #f472b6, #34d399, #fbbf24, #f87171, #38bdf8, #2dd4bf, #e879f9
LANE_COLORS_BGR: list[tuple[int, int, int]] = [
    (248, 140, 129),  # indigo  #818cf8
    (182, 114, 244),  # pink    #f472b6
    (153, 211, 52),   # emerald #34d399
    (36, 191, 251),   # amber   #fbbf24
    (113, 113, 248),  # red     #f87171
    (248, 189, 56),   # sky     #38bdf8
    (191, 212, 45),   # teal    #2dd4bf
    (249, 121, 232),  # fuchsia #e879f9
]

# Track has no usable lane assignment yet.
NO_LANE_COLOR_BGR: tuple[int, int, int] = (100, 100, 100)


def build_lane_color_map(lane_ids: list[str]) -> dict[str, tuple[int, int, int]]:
    """Map each lane id to a stable BGR color by config order."""
    return {
        str(lid): LANE_COLORS_BGR[i % len(LANE_COLORS_BGR)]
        for i, lid in enumerate(lane_ids)
        if lid is not None and str(lid).strip()
    }


def resolve_track_lane_id(
    stable_lane: str | None = None,
    raw_lane: str | None = None,
) -> str | None:
    """Pick the best lane id for coloring a track.

    Prefer the smoothed stable lane; fall back to the current raw assignment
    so vehicles already inside a polygon color immediately.
    """
    for candidate in (stable_lane, raw_lane):
        if not candidate:
            continue
        lid = str(candidate).strip()
        if not lid or lid.lower() in {"none", "unknown", "null"}:
            continue
        return lid
    return None


def color_for_lane(
    lane_id: str | None,
    color_map: dict[str, tuple[int, int, int]],
) -> tuple[int, int, int]:
    """Return the BGR color for a lane id, or gray when unknown/unassigned."""
    if not lane_id:
        return NO_LANE_COLOR_BGR
    return color_map.get(str(lane_id), NO_LANE_COLOR_BGR)


def color_for_track(
    color_map: dict[str, tuple[int, int, int]],
    *,
    stable_lane: str | None = None,
    raw_lane: str | None = None,
) -> tuple[int, int, int]:
    """BGR color for a track from stable/raw lane assignment."""
    return color_for_lane(resolve_track_lane_id(stable_lane, raw_lane), color_map)
