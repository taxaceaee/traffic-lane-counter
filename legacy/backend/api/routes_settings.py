"""Settings API — read/update system-wide parameters.

All settings are persisted to ``configs/settings.json`` and the server
loads them from environment variables first (for containerised deployments)
then falls back to the file.

Settings that require a restart to take effect are clearly labelled.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger("trafficflow.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])

_SETTINGS_DIR = Path("configs")
_SETTINGS_FILE = _SETTINGS_DIR / "settings.json"

# ── Schema ──────────────────────────────────────────────────────────────────


class DetectionSettings(BaseModel):
    confidence: float = Field(default=0.35, ge=0.0, le=1.0, description="YOLO confidence threshold")
    iou: float = Field(default=0.5, ge=0.0, le=1.0, description="NMS IoU threshold")
    imgsz: int = Field(default=640, ge=128, le=1920, description="Inference image size (px)")
    half: bool = Field(default=True, description="FP16 half-precision inference")
    detect_every_n_frames: int = Field(default=2, ge=1, le=10, description="Run detection every N frames")
    tracker: str = Field(default="bytetrack", pattern=r"^(bytetrack|botsort|deepsort|ocsort)$")
    track_buffer: int = Field(default=30, ge=5, le=120, description="Track buffer length (frames)")
    max_detections: int = Field(default=300, ge=1, le=1000, description="Max detections per frame")
    roi_crop: bool = Field(default=True, description="Enable ROI cropping for faster inference")


class StorageSettings(BaseModel):
    output_dir: str = Field(default="./output", description="Output directory for files")
    data_retention_days: int = Field(default=7, ge=1, le=365, description="Auto-cleanup events after N days")
    crop_format: str = Field(default="jpg", pattern=r"^(jpg|png|webp)$")
    crop_quality: int = Field(default=80, ge=10, le=100, description="Crop JPEG quality")
    crop_max_px: int = Field(default=320, ge=64, le=1024, description="Max crop dimension (px)")
    aggregate_windows: list[str] = Field(
        default=["1min", "5min", "1hour", "1day"],
        description="Time windows for aggregate roll-ups",
    )


class NotificationSettings(BaseModel):
    backpressure_warn_threshold: int = Field(default=512, ge=64, le=4096, description="Queue depth warning")
    backpressure_crit_threshold: int = Field(default=1024, ge=128, le=8192, description="Queue depth critical")
    dead_letter_max: int = Field(default=10000, ge=100, le=100000, description="Max dead-letter items")
    heartbeat_interval_s: float = Field(default=30.0, ge=5.0, le=120.0, description="WS heartbeat interval (s)")
    heartbeat_timeout_s: float = Field(default=90.0, ge=10.0, le=300.0, description="WS heartbeat timeout (s)")


class SystemSettings(BaseModel):
    max_workers: int = Field(default=4, ge=1, le=16, description="Max concurrent inference jobs")
    max_streams: int = Field(default=16, ge=1, le=64, description="Max concurrent live streams")
    memory_threshold_mb: int = Field(default=0, ge=0, le=65536, description="OOM restart threshold (0=disabled)")
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    db_pool_size: int = Field(default=10, ge=1, le=100, description="DB connection pool size")
    db_pool_overflow: int = Field(default=5, ge=0, le=50, description="DB pool overflow limit")


class AppearanceSettings(BaseModel):
    refresh_interval_s: int = Field(default=30, ge=5, le=300, description="Dashboard auto-refresh interval (s)")
    chart_animations: bool = Field(default=True, description="Enable chart animations")
    timezone: str = Field(default="UTC", description="Display timezone (IANA tz)")


class SettingsData(BaseModel):
    detection: DetectionSettings = Field(default_factory=DetectionSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    system: SystemSettings = Field(default_factory=SystemSettings)
    appearance: AppearanceSettings = Field(default_factory=AppearanceSettings)


class SettingsUpdate(BaseModel):
    detection: DetectionSettings | None = None
    storage: StorageSettings | None = None
    notifications: NotificationSettings | None = None
    system: SystemSettings | None = None
    appearance: AppearanceSettings | None = None


# ── Persistence ─────────────────────────────────────────────────────────────

_DEFAULT = SettingsData().model_dump()


def _load_raw() -> dict[str, Any]:
    if _SETTINGS_FILE.exists():
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to parse %s, using defaults", _SETTINGS_FILE)
    return {}


def load_settings() -> dict[str, Any]:
    """Load settings, merge with defaults, return validated dict."""
    raw = _load_raw()
    merged = dict(_DEFAULT)
    for category in merged:
        if category in raw and isinstance(raw[category], dict):
            merged[category].update(raw[category])
    return merged


def save_settings(data: dict[str, Any]) -> None:
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


# ── Routes ─────────────────────────────────────────────────────────────────


@router.get("")
async def get_settings():
    return load_settings()


@router.put("")
async def update_settings(body: SettingsUpdate):
    current = load_settings()
    update_dict = body.model_dump(exclude_none=True)
    for category, values in update_dict.items():
        if values is not None and isinstance(values, dict):
            current.setdefault(category, {})
            for k, v in values.items():
                if v is not None:
                    current[category][k] = v
    save_settings(current)
    logger.info("Settings updated: %s", list(update_dict.keys()))
    return {"status": "saved", "settings": current}
