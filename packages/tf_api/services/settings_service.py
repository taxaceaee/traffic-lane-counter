"""Runtime settings service for tf_api.

Reads ``configs/settings.json`` and overlays values on top of repo defaults.
This keeps the Settings UI and live runtime on the same source of truth.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

logger = logging.getLogger("trafficflow.settings_service")

_SETTINGS_PATH = Path("configs/settings.json")

_DEFAULTS: dict[str, Any] = {
    "api_url": "http://localhost:8000",
    "detection": {
        # Realtime defaults for multi-cam laptop GPUs (recall still high via
        # LIVE_MAX_IMGSZ + ROI crop; cadence adapts via LIVE_RECALL_MODE=fps).
        "confidence": 0.25,
        "iou": 0.45,
        "imgsz": 960,
        "half": True,
        "detect_every_n_frames": 1,
        "tracker": "bytetrack",
        "track_buffer": 45,
        "max_detections": 300,
        "roi_crop": True,
        "roi_padding": 80,
    },
    # Preview encode budget — keep Output FPS near Process FPS.
    "preview": {
        "jpeg_quality": 62,
        "target_fps": 12,
        "preserve_source_resolution": True,
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
        "chart_animations": True,
        "timezone": "UTC",
    },
}


def _load_raw() -> dict[str, Any]:
    if _SETTINGS_PATH.exists():
        try:
            with open(_SETTINGS_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
                return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to parse %s, using defaults", _SETTINGS_PATH, exc_info=True)
    return {}


def get_settings() -> dict[str, Any]:
    raw = _load_raw()
    merged: dict[str, Any] = {}
    for key, default in _DEFAULTS.items():
        if isinstance(default, dict):
            merged[key] = dict(default)
            if isinstance(raw.get(key), dict):
                merged[key].update({k: v for k, v in raw[key].items() if v is not None})
        else:
            merged[key] = raw.get(key, default)
    return merged


def get_default_settings() -> dict[str, Any]:
    return deepcopy(_DEFAULTS)


def get_detection_defaults() -> dict[str, Any]:
    return dict(get_settings().get("detection", _DEFAULTS["detection"]))


def get_preview_defaults() -> dict[str, Any]:
    """Live MJPEG preview knobs (full-res, paced encode)."""
    return dict(get_settings().get("preview", _DEFAULTS["preview"]))


def get_system_config() -> dict[str, Any]:
    return dict(get_settings().get("system", _DEFAULTS["system"]))


def get_max_workers() -> int:
    return int(get_system_config().get("max_workers", 4))


def get_max_streams() -> int:
    return int(get_system_config().get("max_streams", 16))
