"""Guard against regressing to stale detection/preview defaults."""

from pathlib import Path

from tf_api.services.settings_service import get_detection_defaults, get_preview_defaults
from tf_core.io.config_loader import load_and_validate_config


def test_settings_detection_defaults_are_max_recall():
    d = get_detection_defaults()
    assert d["detect_every_n_frames"] == 1
    assert d["confidence"] == 0.25
    assert d["iou"] == 0.45
    assert d["max_detections"] == 500
    assert d["roi_crop"] is True
    assert d["roi_padding"] == 80


def test_preview_defaults_full_res_paced():
    p = get_preview_defaults()
    assert p["preserve_source_resolution"] is True
    assert int(p["jpeg_quality"]) >= 60
    assert float(p["target_fps"]) >= 8


def test_config_loader_detector_defaults_match_settings(tmp_path):
    """Offline pipeline defaults must not reintroduce detect_every_n=2 / conf=0.35."""
    cfg_path = tmp_path / "minimal.yaml"
    cfg_path.write_text(
        """
frame_size: {width: 1280, height: 720}
coordinate_space: original_frame
class_modes:
  coco_pretrained: [car, motorcycle, bus, truck]
detector:
  weights: weights/yolo11n.pt
  class_mode: coco_pretrained
  allowed_classes: [car, motorcycle, bus, truck]
tracking:
  tracker: bytetrack.yaml
  active_track_timeout_frames: 15
  min_track_age_frames: 2
lane_assignment:
  boundary_mode: inside_or_on_edge
  unknown_policy: keep_last_stable
  unknown_timeout_frames: 15
smoothing:
  method: hybrid
  history_window: 5
  min_consecutive_for_change: 1
lanes: []
""",
        encoding="utf-8",
    )
    cfg = load_and_validate_config(str(cfg_path))
    det = cfg["detector"]
    assert det["detect_every_n_frames"] == 1
    assert float(det["conf"]) == 0.25
    assert float(det["iou"]) == 0.45
    assert int(det["max_detections"]) == 500
    assert int(det["imgsz"]) == 1280


def test_no_legacy_benchmark_packages():
    root = Path(".")
    assert not (root / "tf_worker" / "evaluation").exists()
    assert not (root / "tf_worker" / "benchmark").exists()
    assert not (root / "scripts" / "run_benchmark.py").exists()
    assert not (root / "scripts" / "benchmark_model.py").exists()


def test_live_module_has_current_preview_path():
    src = Path("tf_api/api/routes_live.py").read_text(encoding="utf-8")
    assert "_encode_work_q" not in src
    assert "idle_for" in src
    assert 'stream_meta["occupancy"]' in src
    assert "Queue(maxsize=1)" in src

    js = Path("frontend/js/pages/live.js").read_text(encoding="utf-8")
    assert "_stopMJPEGStream" in js
    assert "_isLiveMessageForCamera" in js
    assert "_liveLoadGeneration" in js


def test_frontend_asset_cache_busters_are_unified():
    index = Path("frontend/index.html").read_text(encoding="utf-8")
    stamps = sorted(set(
        line.split("?t=")[1].split('"')[0]
        for line in index.splitlines()
        if ".js?t=" in line
    ))
    assert len(stamps) == 1, f"mixed cache busters remain: {stamps}"
