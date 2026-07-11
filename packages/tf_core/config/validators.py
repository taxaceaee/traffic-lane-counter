from collections.abc import Iterable
from pathlib import Path

from tf_core.config.loader import ConfigError, resolve_path
from tf_core.config.schemas import CameraConfig, LaneConfig, ModelsConfig

CLASS_MODES = {
    "coco_pretrained": ["bicycle", "car", "motorcycle", "bus", "truck"],
    "detrac_native": ["car", "van", "bus", "others"],
}


def _ensure_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise ConfigError(f"{label} does not exist: {path}")


def validate_models_config(models_config: ModelsConfig, base_dir: str | Path = ".") -> None:
    seen = set()
    for model in models_config.models:
        if model.model_id in seen:
            raise ConfigError(f"Duplicate model_id in models config: {model.model_id}")
        seen.add(model.model_id)
        if model.class_mode not in CLASS_MODES:
            raise ConfigError(f"Unsupported class_mode for model {model.model_id}: {model.class_mode}")
        _ensure_exists(resolve_path(model.path, base_dir), f"Model path for {model.model_id}")


def _validate_counting_line(lane_id: str, counting_line, width: int, height: int) -> None:
    """Validate counting_line structure: start, end, direction_ref must be [x, y] within frame."""
    if counting_line is None:
        return
    from tf_core.config.schemas import CountingLineDef

    if not isinstance(counting_line, (dict, CountingLineDef)):
        raise ConfigError(f"Lane {lane_id} counting_line must be a dict with 'start', 'end', 'direction_ref'")
    if isinstance(counting_line, dict):
        for field in ("start", "end", "direction_ref"):
            if field not in counting_line:
                raise ConfigError(f"Lane {lane_id} counting_line missing field: {field}")
            pt = counting_line[field]
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise ConfigError(f"Lane {lane_id} counting_line.{field} must be [x, y]")
            x, y = pt
            if not (0 <= x <= width and 0 <= y <= height):
                raise ConfigError(
                    f"Lane {lane_id} counting_line.{field} [{x}, {y}] is outside frame {width}x{height}"
                )
        return
    # CountingLineDef instance
    for label, pt in (("start", counting_line.start), ("end", counting_line.end),
                       ("direction_ref", counting_line.direction_ref)):
        if len(pt) != 2:
            raise ConfigError(f"Lane {lane_id} counting_line.{label} must be [x, y]")
        x, y = pt
        if not (0 <= x <= width and 0 <= y <= height):
            raise ConfigError(f"Lane {lane_id} counting_line.{label} [{x}, {y}] is outside frame {width}x{height}")


def validate_lane_config(lane_config: LaneConfig) -> None:
    width = lane_config.frame_size.width
    height = lane_config.frame_size.height
    lane_ids = set()
    for lane in lane_config.lanes:
        if lane.lane_id in lane_ids:
            raise ConfigError(f"Duplicate lane_id in lane config: {lane.lane_id}")
        lane_ids.add(lane.lane_id)
        if len(lane.polygon) < 3:
            raise ConfigError(f"Lane {lane.lane_id} polygon must contain at least 3 points")
        for idx, point in enumerate(lane.polygon):
            if len(point) != 2:
                raise ConfigError(f"Lane {lane.lane_id} point {idx} must be [x, y]")
            x, y = point
            if not (0 <= x <= width and 0 <= y <= height):
                raise ConfigError(
                    f"Lane {lane.lane_id} point {idx} [{x}, {y}] is outside frame {width}x{height}"
                )
        _validate_counting_line(lane.lane_id, lane.counting_line, width, height)


def validate_camera_config(
    camera_config: CameraConfig,
    models_config: ModelsConfig,
    lane_config: LaneConfig,
    base_dir: str | Path = ".",
) -> None:
    model_ids = {model.model_id for model in models_config.models}
    if camera_config.model.model_id not in model_ids:
        raise ConfigError(f"Camera references unknown model_id: {camera_config.model.model_id}")

    if camera_config.camera.source_type in {"video", "image_dir"}:
        _ensure_exists(resolve_path(camera_config.camera.source, base_dir), "Camera source")

    if lane_config.camera_id != camera_config.camera.camera_id:
        raise ConfigError(
            f"Lane config camera_id {lane_config.camera_id} does not match camera "
            f"{camera_config.camera.camera_id}"
        )

    camera_size = camera_config.camera.frame_size
    lane_size = lane_config.frame_size
    if camera_size.width != lane_size.width or camera_size.height != lane_size.height:
        raise ConfigError(
            f"Lane config frame_size {lane_size.width}x{lane_size.height} does not match "
            f"camera frame_size {camera_size.width}x{camera_size.height}"
        )

    model = next(item for item in models_config.models if item.model_id == camera_config.model.model_id)
    allowed = camera_config.model.allowed_classes
    if allowed is not None:
        validate_allowed_classes(allowed, model.class_mode)

    occ = camera_config.occupancy
    if occ.min_consecutive_for_change > occ.history_window:
        raise ConfigError("min_consecutive_for_change cannot be larger than history_window")


def validate_allowed_classes(classes: Iterable[str], class_mode: str) -> None:
    supported = set(CLASS_MODES[class_mode])
    invalid = [name for name in classes if name not in supported]
    if invalid:
        raise ConfigError(f"allowed_classes {invalid} are not valid for class_mode {class_mode}")
