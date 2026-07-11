"""Settings API — read/update system-wide parameters."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from tf_api.api.routes_auth import get_current_user, require_admin
from tf_api.services.settings_service import get_default_settings, get_settings

logger = logging.getLogger("trafficflow.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])

_SETTINGS_DIR = Path("configs")
_SETTINGS_FILE = _SETTINGS_DIR / "settings.json"


class DetectionSettings(BaseModel):
    # Keep Field defaults aligned with settings_service (single source of truth).
    confidence: float = Field(default=0.22, ge=0.0, le=1.0)
    iou: float = Field(default=0.5, ge=0.0, le=1.0)
    imgsz: int = Field(default=960, ge=128, le=1920)
    half: bool = Field(default=True)
    detect_every_n_frames: int = Field(default=1, ge=1, le=10)
    tracker: str = Field(default="bytetrack")
    track_buffer: int = Field(default=30, ge=5, le=120)
    max_detections: int = Field(default=300, ge=1, le=1000)
    roi_crop: bool = Field(default=True)


class StorageSettings(BaseModel):
    output_dir: str = Field(default="./output")
    data_retention_days: int = Field(default=7, ge=1, le=365)
    crop_format: str = Field(default="jpg")
    crop_quality: int = Field(default=80, ge=10, le=100)
    crop_max_px: int = Field(default=320, ge=64, le=1024)
    aggregate_windows: list[str] = Field(default=["1hour", "1day"])


class NotificationSettings(BaseModel):
    backpressure_warn_threshold: int = Field(default=512, ge=64, le=4096)
    backpressure_crit_threshold: int = Field(default=1024, ge=128, le=8192)
    dead_letter_max: int = Field(default=10000, ge=100, le=100000)
    heartbeat_interval_s: float = Field(default=30.0, ge=5.0, le=120.0)
    heartbeat_timeout_s: float = Field(default=90.0, ge=10.0, le=300.0)


class SystemSettings(BaseModel):
    max_workers: int = Field(default=4, ge=1, le=16)
    max_streams: int = Field(default=16, ge=1, le=64)
    memory_threshold_mb: int = Field(default=0, ge=0, le=65536)
    log_level: str = Field(default="INFO")
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_pool_overflow: int = Field(default=5, ge=0, le=50)


class AppearanceSettings(BaseModel):
    refresh_interval_s: int = Field(default=30, ge=5, le=300)
    chart_animations: bool = Field(default=True)
    timezone: str = Field(default="UTC")


class SettingsUpdate(BaseModel):
    api_url: str | None = None
    detection: DetectionSettings | None = None
    storage: StorageSettings | None = None
    notifications: NotificationSettings | None = None
    system: SystemSettings | None = None
    appearance: AppearanceSettings | None = None


def _save_settings(data: dict[str, Any]) -> None:
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = _SETTINGS_FILE.with_suffix(".json.tmp")
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
    temp_file.replace(_SETTINGS_FILE)


@router.get("")
async def read_settings(_user: dict = Depends(get_current_user)):
    return get_settings()


@router.get("/defaults")
async def read_default_settings(_user: dict = Depends(get_current_user)):
    return get_default_settings()


@router.put("")
async def update_settings(body: SettingsUpdate, _user: dict = Depends(require_admin)):
    current = get_settings()
    update_dict = body.model_dump(exclude_none=True)
    for category, values in update_dict.items():
        if isinstance(values, dict):
            current.setdefault(category, {})
            current[category].update({k: v for k, v in values.items() if v is not None})
        else:
            current[category] = values
    _save_settings(current)
    logger.info("Settings updated: %s", sorted(update_dict.keys()))
    return {"status": "saved", "settings": current}


@router.post("/reset")
async def reset_settings(_user: dict = Depends(require_admin)):
    defaults = get_default_settings()
    _save_settings(defaults)
    logger.info("Settings reset to defaults")
    return {"status": "reset", "settings": defaults}
