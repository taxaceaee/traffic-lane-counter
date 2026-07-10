from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when server configuration cannot be loaded or validated."""


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML file and require a mapping at the root."""
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ConfigError(f"Configuration file must contain a mapping: {config_path}")
    return data


def normalize_camera_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a nested camera YAML config into a flat pipeline-ready dict.

    Camera YAML files use a nested structure (``camera:``, ``model:``, etc.)
    but the pipeline expects flat keys like ``camera_id``, ``source`` at the
    top level.  This function promotes the ``camera`` sub-dict and keeps
    other top-level sections (``model``, ``tracker``, ``lanes`` etc.) as-is.
    """
    cfg = {**raw}
    camera_section = cfg.pop("camera", {})
    if isinstance(camera_section, dict):
        for k, v in camera_section.items():
            if k not in cfg:
                cfg[k] = v
    return cfg


def write_yaml(path: str | Path, data: dict[str, Any]) -> None:
    """Write a YAML mapping with deterministic key order preserved by insertion."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def resolve_path(path_value: str | Path, base_dir: str | Path | None = None) -> Path:
    """Resolve relative paths against a base directory, defaulting to cwd."""
    path = Path(path_value)
    if path.is_absolute():
        return path
    return Path(base_dir or ".").resolve() / path
