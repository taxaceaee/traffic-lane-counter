"""Lane color helpers used by live annotate and offline visualizer."""

from tf_common.viz_colors import (
    NO_LANE_COLOR_BGR,
    build_lane_color_map,
    color_for_lane,
    color_for_track,
    resolve_track_lane_id,
)


def test_same_lane_maps_to_same_color():
    colors = build_lane_color_map(["lane_1", "lane_2", "lane_3"])
    assert color_for_lane("lane_1", colors) == colors["lane_1"]
    assert color_for_lane("lane_1", colors) != color_for_lane("lane_2", colors)
    assert color_for_lane("lane_2", colors) != color_for_lane("lane_3", colors)


def test_missing_lane_is_gray():
    colors = build_lane_color_map(["lane_1"])
    assert color_for_lane(None, colors) == NO_LANE_COLOR_BGR
    assert color_for_lane("unknown_lane", colors) == NO_LANE_COLOR_BGR


def test_resolve_prefers_stable_then_raw():
    assert resolve_track_lane_id("lane_1", "lane_2") == "lane_1"
    assert resolve_track_lane_id(None, "lane_2") == "lane_2"
    assert resolve_track_lane_id(None, "unknown") is None
    assert resolve_track_lane_id("none", "unknown") is None


def test_color_for_track_groups_by_lane():
    colors = build_lane_color_map(["lane_1", "lane_2"])
    a = color_for_track(colors, stable_lane="lane_1", raw_lane="lane_2")
    b = color_for_track(colors, stable_lane=None, raw_lane="lane_1")
    c = color_for_track(colors, stable_lane="lane_2", raw_lane=None)
    assert a == b == colors["lane_1"]
    assert c == colors["lane_2"]
    assert a != c
