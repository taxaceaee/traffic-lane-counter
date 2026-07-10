"""Settings Service — central runtime consumer of configs/settings.json.

This module is the single bridge between the Settings UI (which writes to
configs/settings.json) and all pipeline/backend code that needs those values.
Every consumer imports from here, never from routes_settings.py directly.

Fallback chain: runtime override > settings.json > code default.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("trafficflow.settings_service")

_SETTINGS_PATH = Path("configs/settings.json")

_DEFAULTS: dict[str, Any] = {
    "detection": {
        "confidence": 0.35,
        "iou": 0.5,
        "imgsz": 640,
        "half": True,
        "detect_every_n_frames": 2,
        "tracker": "bytetrack",
        "track_buffer": 30,
        "max_detections": 300,
        "roi_crop": True,
    },
    "storage": {
        "output_dir": "./output",
        "data_retention_days": 7,
        "crop_format": "jpg",
        "crop_quality": 80,
        "crop_max_px": 320,
        "aggregate_windows": ["1min", "5min", "1hour", "1day"],
    },
    "notifications": {
        "backpressure_warn_threshold": 512,
        "backpressure_crit_threshold": 1024,
        "dead_letter_max": 10000,
        "heartbeat_interval_s": 30.0,
        "heartbeat_timeout_s": 90.0,
    },
    "system": {
        "max_workers": 4,
        "max_streams": 16,
        "memory_threshold_mb": 0,
        "log_level": "INFO",
        "db_pool_size": 10,
        "db_pool_overflow": 5,
    },
    "appearance": {
        "refresh_interval_s": 30,
        "timezone": "UTC",
        "chart_animations": True,
    },
}


def _load_raw() -> dict[str, Any]:
    if _SETTINGS_PATH.exists():
        try:
            with open(_SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to parse %s, using defaults", _SETTINGS_PATH)
    return {}


def get_settings() -> dict[str, Any]:
    """Return merged settings: defaults overlaid with user overrides."""
    raw = _load_raw()
    merged = {}
    for category, defaults in _DEFAULTS.items():
        merged[category] = dict(defaults)
        if category in raw and isinstance(raw[category], dict):
            merged[category].update(
                {k: v for k, v in raw[category].items() if v is not None}
            )
    return merged


def get_detection_defaults() -> dict[str, Any]:
    """Detection defaults from settings.json to use as fallback in pipeline configs."""
    s = get_settings()
    return dict(s.get("detection", _DEFAULTS["detection"]))


def get_storage_retention_days() -> int:
    return get_settings().get("storage", {}).get("data_retention_days", 7)


def get_notification_thresholds() -> dict[str, Any]:
    return get_settings().get("notifications", _DEFAULTS["notifications"])


def get_system_config() -> dict[str, Any]:
    return get_settings().get("system", _DEFAULTS["system"])


def get_appearance_config() -> dict[str, Any]:
    return get_settings().get("appearance", _DEFAULTS["appearance"])


def get_log_level() -> str:
    """Return log level from settings or env var (env wins for containerized)."""
    return os.getenv("LOG_LEVEL", "") or get_system_config().get("log_level", "INFO")


def get_max_workers() -> int:
    return get_system_config().get("max_workers", 4)


def get_max_streams() -> int:
    return get_system_config().get("max_streams", 16)
