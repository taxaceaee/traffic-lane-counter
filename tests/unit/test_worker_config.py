from pathlib import Path

from tf_core.config.compiler import compile_camera_config
from tf_worker.worker import _build_pipeline_config


def test_worker_preserves_compiled_pipeline_sections():
    camera_path = Path("configs/cameras/YT_LIVE_TEST.yaml")
    compiled = compile_camera_config(camera_path, job_id="test")

    pipeline = _build_pipeline_config(compiled, {})

    assert pipeline["detector"]["weights"].endswith("weights/yolo11n.pt")
    assert "tracking" in pipeline
    assert "lane_assignment" in pipeline
    assert "smoothing" in pipeline
    assert isinstance(pipeline["lanes"], list)
    assert pipeline["server"]["job_id"] == "test"
