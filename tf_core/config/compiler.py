import torch
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tf_core.config.loader import ConfigError, load_yaml, resolve_path, write_yaml
from tf_core.config.schemas import AppConfig, CameraConfig, LaneConfig, ModelsConfig
from tf_core.config.validators import (
    CLASS_MODES,
    validate_camera_config,
    validate_lane_config,
    validate_models_config,
)


def _parse_schema(schema_cls, data: dict[str, Any], label: str):
    try:
        return schema_cls(**data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid {label}: {exc}") from exc


def load_server_configs(
    camera_config_path: str | Path,
    app_config_path: str | Path = "configs/app.yaml",
    models_config_path: str | Path = "configs/models.yaml",
) -> tuple[AppConfig, ModelsConfig, CameraConfig, LaneConfig]:
    """Load and validate the app, models, camera, and lane config files."""
    camera_path = Path(camera_config_path)
    app_path = Path(app_config_path)
    models_path = Path(models_config_path)

    app_config = _parse_schema(AppConfig, load_yaml(app_path), "app config")
    models_config = _parse_schema(ModelsConfig, load_yaml(models_path), "models config")
    camera_config = _parse_schema(CameraConfig, load_yaml(camera_path), "camera config")

    lane_config_path = resolve_path(camera_config.lanes.config_path, camera_path.parent)
    lane_config = _parse_schema(LaneConfig, load_yaml(lane_config_path), "lane config")

    repo_root = Path(".").resolve()
    validate_models_config(models_config, repo_root)
    validate_lane_config(lane_config)
    validate_camera_config(camera_config, models_config, lane_config, repo_root)

    return app_config, models_config, camera_config, lane_config


def compile_camera_config(
    camera_config_path: str | Path,
    app_config_path: str | Path = "configs/app.yaml",
    models_config_path: str | Path = "configs/models.yaml",
    job_id: str = "offline",
) -> dict[str, Any]:
    """Compile server camera/model/lane config into the MVP pipeline config schema."""
    app_config, models_config, camera_config, lane_config = load_server_configs(
        camera_config_path, app_config_path, models_config_path
    )
    model = next(item for item in models_config.models if item.model_id == camera_config.model.model_id)
    output_cfg = camera_config.output or {}
    runtime = app_config.runtime or {}

    allowed_classes = camera_config.model.allowed_classes or CLASS_MODES[model.class_mode]

    compiled = {
        "frame_size": {
            "width": camera_config.camera.frame_size.width,
            "height": camera_config.camera.frame_size.height,
        },
        "coordinate_space": lane_config.coordinate_space,
        "detector": {
            "weights": str(resolve_path(model.path)),
            "imgsz": camera_config.model.imgsz,
            "conf": camera_config.model.conf_threshold,
            "iou": camera_config.model.iou_threshold,
            "class_mode": model.class_mode,
            "allowed_classes": allowed_classes,
            "half": bool(torch.cuda.is_available()),
            "detect_every_n_frames": 2,
            "roi_crop": True,
        },
        "class_modes": CLASS_MODES,
        "tracking": {
            "tracker": camera_config.tracker.tracker,
            "active_track_timeout_frames": camera_config.tracker.track_timeout_frames,
            "min_track_age_frames": camera_config.tracker.min_track_age_frames,
        },
        "lane_assignment": {
            "boundary_mode": "inside_or_on_edge",
            "unknown_policy": "keep_last_stable",
            "unknown_timeout_frames": camera_config.occupancy.unknown_timeout_frames,
        },
        "smoothing": {
            "method": "hybrid",
            "history_window": camera_config.occupancy.history_window,
            "min_consecutive_for_change": camera_config.occupancy.min_consecutive_for_change,
        },
        "input": {
            "source_type": camera_config.camera.source_type,
            "fps": camera_config.camera.fps,
            "image_extensions": [".jpg", ".jpeg", ".png"],
            "image_sorting": "alphabetical",
            "allow_scaling": camera_config.camera.allow_scaling,
        },
        "output": {
            "save_video": bool(output_cfg.get("save_video", runtime.get("save_annotated_video", True))),
            "save_jsonl": bool(output_cfg.get("save_jsonl", True)),
            "save_csv": bool(output_cfg.get("save_csv", True)),
            "video_fps_mode": output_cfg.get("video_fps_mode", "source"),
        },
        "lanes": [
            {
                "id": lane.lane_id,
                "name": lane.name,
                "points": lane.polygon,
                **({"counting_line": lane.counting_line.model_dump()} if lane.counting_line else {}),
            }
            for lane in lane_config.lanes
        ],
        "server": {
            "camera_id": camera_config.camera.camera_id,
            "camera_name": camera_config.camera.name,
            "job_id": job_id,
            "model_id": model.model_id,
            "model_description": model.description,
            "source": camera_config.camera.source,
            "source_type": camera_config.camera.source_type,
        },
    }
    return compiled


def write_compiled_config(output_path: str | Path, compiled_config: dict[str, Any]) -> None:
    write_yaml(output_path, compiled_config)
