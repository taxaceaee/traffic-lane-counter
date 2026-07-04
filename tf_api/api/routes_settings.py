"""Settings API — read/update system and AI model parameters."""

import json
import logging
import os

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("trafficflow.settings")

router = APIRouter(prefix="/api/settings", tags=["settings"])

_SETTINGS_FILE = "configs/settings.json"

_DEFAULT_SETTINGS = {
    "api_url": "http://localhost:8000",
    "max_workers": 4,
    "output_dir": "./output",
    "detection": {
        "confidence": 0.35,
        "iou": 0.5,
        "tracker": "bytetrack",
        "track_buffer": 30,
    },
}


def _load_settings():
    try:
        with open(_SETTINGS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_SETTINGS)


def _save_settings(data):
    os.makedirs(os.path.dirname(_SETTINGS_FILE) or ".", exist_ok=True)
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


class DetectionParams(BaseModel):
    confidence: float = 0.35
    iou: float = 0.5
    tracker: str = "bytetrack"
    track_buffer: int = 30


class SettingsUpdate(BaseModel):
    api_url: str | None = None
    max_workers: int | None = None
    output_dir: str | None = None
    detection: DetectionParams | None = None


@router.get("")
async def get_settings():
    return _load_settings()


@router.put("")
async def update_settings(body: SettingsUpdate):
    settings = _load_settings()
    if body.api_url is not None:
        settings["api_url"] = body.api_url
    if body.max_workers is not None:
        settings["max_workers"] = body.max_workers
    if body.output_dir is not None:
        settings["output_dir"] = body.output_dir
    if body.detection is not None:
        settings["detection"] = body.detection.model_dump()
    _save_settings(settings)
    return {"status": "saved", "settings": settings}
