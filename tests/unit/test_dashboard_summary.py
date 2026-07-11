"""Dashboard summary fusion helpers and response shape."""

from tf_api.api.routes_dashboard import (
    _count_configured_lanes,
    _fixed_window_peaks,
    _merge_type_counts,
    _sum_vehicle_types,
)


def test_sum_and_merge_types():
    assert _sum_vehicle_types({"car": 3, "motorcycle": 2}) == 5
    assert _merge_type_counts({"car": 1}, {"car": 2, "bus": 1}) == {"car": 3, "bus": 1}


def test_configured_lanes_from_yaml():
    n = _count_configured_lanes()
    # Repo ships 3 cameras × 2 lanes in fixtures.
    assert n >= 2


def test_fixed_window_peaks_always_three_labels():
    hourly = {h: 0 for h in range(24)}
    hourly[8] = 5
    hourly[18] = 9
    peaks, off_avg = _fixed_window_peaks(hourly)
    labels = [p["label"] for p in peaks]
    assert labels == ["morning_peak", "evening_peak", "offpeak"]
    assert peaks[0]["count"] == 5
    assert peaks[1]["count"] == 9
    assert isinstance(off_avg, int)
