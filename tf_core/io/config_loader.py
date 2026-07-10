from pathlib import Path
from typing import Any

import yaml


def load_and_validate_config(config_path: str | Path) -> dict[str, Any]:
    """Loads a YAML configuration file and performs strict validation checks.

    Args:
        config_path: Path to the YAML configuration file.

    Returns:
        A dictionary containing the validated configuration.

    Raises:
        ValueError: If any validation rule is violated.
        FileNotFoundError: If the configuration file does not exist.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found at: {path}")

    with open(path) as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Failed to parse YAML configuration: {e}") from e

    if not isinstance(config, dict):
        raise ValueError("Configuration must be a dictionary.")

    # 1. Validate frame_size
    if "frame_size" not in config:
        raise ValueError("Missing 'frame_size' section in configuration.")
    frame_size = config["frame_size"]
    if not isinstance(frame_size, dict) or "width" not in frame_size or "height" not in frame_size:
        raise ValueError("'frame_size' must contain 'width' and 'height'.")
    width = frame_size["width"]
    height = frame_size["height"]
    if not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
        raise ValueError("'frame_size' width and height must be positive integers.")

    # 2. Validate coordinate_space
    coord_space = config.get("coordinate_space", "original_frame")
    if coord_space != "original_frame":
        raise ValueError(f"Unsupported coordinate_space: '{coord_space}'. Must be 'original_frame'.")
    config["coordinate_space"] = coord_space

    # 3. Validate class_modes
    if "class_modes" not in config:
        # Provide default class modes if not specified
        config["class_modes"] = {
            "coco_pretrained": ["bicycle", "car", "motorcycle", "bus", "truck"],
            "detrac_native": ["car", "van", "bus", "others"]
        }
    class_modes = config["class_modes"]
    if not isinstance(class_modes, dict):
        raise ValueError("'class_modes' must be a dictionary mapping mode names to list of class names.")
    for mode, classes in class_modes.items():
        if not isinstance(classes, list) or not all(isinstance(c, str) for c in classes):
            raise ValueError(f"Classes for mode '{mode}' in 'class_modes' must be a list of strings.")

    # 4. Validate detector
    if "detector" not in config:
        raise ValueError("Missing 'detector' section in configuration.")
    detector = config["detector"]
    if not isinstance(detector, dict):
        raise ValueError("'detector' section must be a dictionary.")

    # Defaults for detector
    detector.setdefault("weights", "weights/yolo11n.pt")
    detector.setdefault("imgsz", 960)
    detector.setdefault("half", True)
    detector.setdefault("detect_every_n_frames", 2)
    detector.setdefault("roi_crop", True)
    detector.setdefault("conf", 0.35)
    detector.setdefault("iou", 0.5)
    detector.setdefault("class_mode", "coco_pretrained")

    if not isinstance(detector["weights"], str) or not detector["weights"]:
        raise ValueError("detector.weights must be a non-empty string.")
    if not isinstance(detector["imgsz"], int) or detector["imgsz"] <= 0:
        raise ValueError("detector.imgsz must be a positive integer.")
    if not isinstance(detector["conf"], (int, float)) or not (0.0 <= detector["conf"] <= 1.0):
        raise ValueError("detector.conf must be a float between 0.0 and 1.0.")
    if not isinstance(detector["iou"], (int, float)) or not (0.0 <= detector["iou"] <= 1.0):
        raise ValueError("detector.iou must be a float between 0.0 and 1.0.")

    class_mode = detector["class_mode"]
    if class_mode not in class_modes:
        raise ValueError(f"detector.class_mode '{class_mode}' is not defined in class_modes.")

    # Populate allowed_classes if not explicitly provided
    allowed_classes = detector.get("allowed_classes", class_modes[class_mode])
    if not isinstance(allowed_classes, list) or not all(c in class_modes[class_mode] for c in allowed_classes):
        raise ValueError(f"detector.allowed_classes must be a subset of class_modes.{class_mode}: {class_modes[class_mode]}")
    detector["allowed_classes"] = allowed_classes

    # 5. Validate tracking
    if "tracking" not in config:
        raise ValueError("Missing 'tracking' section in configuration.")
    tracking = config["tracking"]
    if not isinstance(tracking, dict):
        raise ValueError("'tracking' section must be a dictionary.")

    tracking.setdefault("tracker", "bytetrack.yaml")
    tracking.setdefault("active_track_timeout_frames", 10)
    tracking.setdefault("min_track_age_frames", 3)

    if not isinstance(tracking["tracker"], str) or not tracking["tracker"]:
        raise ValueError("tracking.tracker must be a non-empty string.")
    if not isinstance(tracking["active_track_timeout_frames"], int) or tracking["active_track_timeout_frames"] <= 0:
        raise ValueError("tracking.active_track_timeout_frames must be a positive integer.")
    if not isinstance(tracking["min_track_age_frames"], int) or tracking["min_track_age_frames"] < 0:
        raise ValueError("tracking.min_track_age_frames must be a non-negative integer.")

    # 6. Validate lane_assignment
    if "lane_assignment" not in config:
        raise ValueError("Missing 'lane_assignment' section in configuration.")
    lane_assignment = config["lane_assignment"]
    if not isinstance(lane_assignment, dict):
        raise ValueError("'lane_assignment' section must be a dictionary.")

    lane_assignment.setdefault("boundary_mode", "inside_or_on_edge")
    lane_assignment.setdefault("unknown_policy", "keep_last_stable")
    lane_assignment.setdefault("unknown_timeout_frames", 15)

    if lane_assignment["boundary_mode"] not in ["inside_or_on_edge", "inside_only"]:
        raise ValueError("lane_assignment.boundary_mode must be 'inside_or_on_edge' or 'inside_only'.")
    if lane_assignment["unknown_policy"] != "keep_last_stable":
        raise ValueError("lane_assignment.unknown_policy must be 'keep_last_stable'.")
    if not isinstance(lane_assignment["unknown_timeout_frames"], int) or lane_assignment["unknown_timeout_frames"] <= 0:
        raise ValueError("lane_assignment.unknown_timeout_frames must be a positive integer.")

    # 7. Validate smoothing
    if "smoothing" not in config:
        raise ValueError("Missing 'smoothing' section in configuration.")
    smoothing = config["smoothing"]
    if not isinstance(smoothing, dict):
        raise ValueError("'smoothing' section must be a dictionary.")

    smoothing.setdefault("method", "hybrid")
    smoothing.setdefault("history_window", 10)
    smoothing.setdefault("min_consecutive_for_change", 5)

    if smoothing["method"] != "hybrid":
        raise ValueError("smoothing.method must be 'hybrid'.")
    if not isinstance(smoothing["history_window"], int) or smoothing["history_window"] <= 0:
        raise ValueError("smoothing.history_window must be a positive integer.")
    if not isinstance(smoothing["min_consecutive_for_change"], int) or smoothing["min_consecutive_for_change"] <= 0:
        raise ValueError("smoothing.min_consecutive_for_change must be a positive integer.")
    if smoothing["min_consecutive_for_change"] > smoothing["history_window"]:
        raise ValueError("smoothing.min_consecutive_for_change cannot be larger than smoothing.history_window.")

    # 8. Validate input (optional)
    config.setdefault("input", {})
    inp = config["input"]
    if not isinstance(inp, dict):
        raise ValueError("'input' section must be a dictionary.")
    inp.setdefault("source_type", "auto")
    inp.setdefault("image_extensions", [".jpg", ".jpeg", ".png"])
    inp.setdefault("image_sorting", "alphabetical")
    inp.setdefault("fps", 25)

    if inp["source_type"] not in ["auto", "video", "image_dir", "rtsp", "youtube", "youtube_live"]:
        raise ValueError(
            "input.source_type must be one of 'auto', 'video', 'image_dir', "
            "'rtsp', 'youtube', or 'youtube_live'."
        )
    if not isinstance(inp["image_extensions"], list) or not all(isinstance(ext, str) for ext in inp["image_extensions"]):
        raise ValueError("input.image_extensions must be a list of strings.")
    if inp["image_sorting"] != "alphabetical":
        raise ValueError("input.image_sorting must be 'alphabetical'.")
    if not isinstance(inp["fps"], (int, float)) or inp["fps"] <= 0:
        raise ValueError("input.fps must be a positive number.")

    # 9. Validate output (optional)
    config.setdefault("output", {})
    out = config["output"]
    if not isinstance(out, dict):
        raise ValueError("'output' section must be a dictionary.")
    out.setdefault("save_video", True)
    out.setdefault("save_jsonl", True)
    out.setdefault("save_csv", True)
    out.setdefault("video_fps_mode", "source")

    # 10. Validate lanes
    if "lanes" not in config:
        raise ValueError("Missing 'lanes' section in configuration.")
    lanes = config["lanes"]
    # Empty lane sets are valid for an unconfigured camera: detection and
    # tracking can still run, while counting simply emits no lane events.
    if not isinstance(lanes, list):
        raise ValueError("'lanes' must be a list.")

    for i, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            raise ValueError(f"Lane at index {i} must be a dictionary.")
        if "id" not in lane or not isinstance(lane["id"], str) or not lane["id"]:
            raise ValueError(f"Lane at index {i} must have a non-empty 'id' string.")
        if "points" not in lane:
            raise ValueError(f"Lane '{lane['id']}' is missing 'points'.")
        points = lane["points"]
        if not isinstance(points, list) or len(points) < 3:
            raise ValueError(f"Lane '{lane['id']}' points must be a list of at least 3 points.")

        for pt_idx, pt in enumerate(points):
            if not isinstance(pt, list) or len(pt) != 2:
                raise ValueError(f"Lane '{lane['id']}' point at index {pt_idx} must be a list of [x, y].")
            x, y = pt
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                raise ValueError(f"Lane '{lane['id']}' point at index {pt_idx} coordinates must be numbers.")
            # Ensure coordinates are within frame_size
            if not (0 <= x <= width) or not (0 <= y <= height):
                raise ValueError(
                    f"Lane '{lane['id']}' point at index {pt_idx} coordinates [{x}, {y}] "
                    f"are outside frame boundaries [0, 0] to [{width}, {height}]."
                )

        # 10b. Validate optional per-lane counting_line
        if "counting_line" in lane and lane["counting_line"] is not None:
            _validate_counting_line(lane["id"], lane["counting_line"], width, height)

    # 11. Validate optional top-level counting block
    counting = config.setdefault("counting", {})
    if not isinstance(counting, dict):
        raise ValueError("'counting' section must be a dictionary.")
    counting.setdefault("min_cross_distance_px", 2.0)
    counting.setdefault("count_unstable_lane", True)
    if not isinstance(counting["min_cross_distance_px"], (int, float)) or counting["min_cross_distance_px"] < 0:
        raise ValueError("counting.min_cross_distance_px must be a non-negative number.")
    if not isinstance(counting["count_unstable_lane"], bool):
        raise ValueError("counting.count_unstable_lane must be a boolean.")

    return config


def _validate_counting_line(lane_id: str, cl: dict, width: int, height: int) -> None:
    """Structural validation only -- checks point count, types, and frame bounds.

    Runtime collinearity check (in_sign == 0) is done in CountingLine.__init__
    to avoid duplicating logic here.

    Hard-rejects: missing/invalid endpoints, zero-length lines, out-of-frame points,
    or a direction_ref collinear with the line (direction would be undefined).
    """
    if not isinstance(cl, dict):
        raise ValueError(f"Lane '{lane_id}' counting_line must be a dictionary.")
    for key in ("start", "end", "direction_ref"):
        if key not in cl:
            raise ValueError(f"Lane '{lane_id}' counting_line is missing '{key}'.")
        pt = cl[key]
        if not isinstance(pt, list) or len(pt) != 2:
            raise ValueError(f"Lane '{lane_id}' counting_line.{key} must be [x, y].")
        x, y = pt
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError(f"Lane '{lane_id}' counting_line.{key} coordinates must be numbers.")
        if not (0 <= x <= width) or not (0 <= y <= height):
            raise ValueError(
                f"Lane '{lane_id}' counting_line.{key} [{x}, {y}] is outside frame "
                f"[0, 0] to [{width}, {height}]."
            )

    sx, sy = cl["start"]
    ex, ey = cl["end"]
    if sx == ex and sy == ey:
        raise ValueError(f"Lane '{lane_id}' counting_line: start and end are identical (zero-length line).")

    # Reject direction_ref collinear with the line -- in_sign would be 0 (undefined).
    rx, ry = cl["direction_ref"]
    cross = (ex - sx) * (ry - sy) - (ey - sy) * (rx - sx)
    if cross == 0:
        raise ValueError(
            f"Lane '{lane_id}' counting_line.direction_ref [{rx}, {ry}] is collinear "
            f"with the line ({sx},{sy})->({ex},{ey}); direction would be undefined."
        )
