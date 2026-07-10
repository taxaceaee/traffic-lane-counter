"""Detection Zone API — define crop regions for model inference.

Each camera can have one or more detection zones (polygons). When configured,
frames are cropped to the* union* of all zone polygons before being sent to
the detection/tracking pipeline, reducing inference area and false positives.

Stored separately from lanes at: configs/detection_zones/{camera_id}_zones.yaml
"""

import contextlib
import fcntl
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.io.safe_path import safe_join, validate_identifier

logger = logging.getLogger("trafficflow.zones")

router = APIRouter(prefix="/api/cameras", tags=["zones"])

_CONFIGS_DIR = Path("configs")
_ZONES_DIR = _CONFIGS_DIR / "detection_zones"


class ZoneUpdateItem(BaseModel):
    zone_id: str
    name: str = ""
    polygon: list[list[float]]


class ZoneUpdateRequest(BaseModel):
    zones: list[ZoneUpdateItem]


def _get_zones_path(camera_id: str) -> Path:
    validate_identifier(camera_id, name="camera_id")
    return safe_join(_ZONES_DIR, f"{camera_id}_zones.yaml")


def _load_camera_config(camera_id: str) -> dict[str, Any] | None:
    cam_path = safe_join(_CONFIGS_DIR / "cameras", f"{camera_id}.yaml")
    if not cam_path.exists():
        return None
    with open(cam_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _get_frame_size(camera_id: str) -> tuple[int, int]:
    cfg = _load_camera_config(camera_id)
    if cfg is None:
        return 960, 540
    camera = cfg.get("camera", {})
    fs = camera.get("frame_size", {})
    return fs.get("width", 960), fs.get("height", 540)


def _write_zones_yaml_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".yaml.lock")
    lock_fh = open(lock_path, "w")
    unique_suffix = f".yaml.tmp.{os.getpid()}"
    tmp = path.with_suffix(unique_suffix)
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False)
            f.flush()
        with contextlib.suppress(OSError):
            tmp.replace(path)
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()
        with contextlib.suppress(OSError):
            tmp.unlink()


@router.get("/{camera_id}/zones")
async def get_zones(camera_id: str):
    """Get detection zones for a camera."""
    validate_identifier(camera_id, name="camera_id")

    zones_path = _get_zones_path(camera_id)
    if not zones_path.exists():
        return []

    with open(zones_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    zones = []
    for item in raw.get("zones", []):
        zones.append({
            "zone_id": item.get("zone_id", ""),
            "name": item.get("name", ""),
            "polygon": item.get("polygon", []),
        })
    return zones


@router.put("/{camera_id}/zones")
async def update_zones(camera_id: str, req: ZoneUpdateRequest):
    """Update detection zones for a camera. Saves to YAML atomically."""
    validate_identifier(camera_id, name="camera_id")
    width, height = _get_frame_size(camera_id)

    zone_ids = set()
    for idx, zone in enumerate(req.zones):
        validate_identifier(zone.zone_id, name=f"zone_id[{idx}]")
        if zone.zone_id in zone_ids:
            raise HTTPException(422, f"Duplicate zone_id: {zone.zone_id}")
        zone_ids.add(zone.zone_id)

        if len(zone.polygon) < 3:
            raise HTTPException(422, f"Zone {zone.zone_id} polygon must have >= 3 points")

        for pt_idx, pt in enumerate(zone.polygon):
            if len(pt) != 2:
                raise HTTPException(422, f"Zone {zone.zone_id} point {pt_idx} must be [x, y]")
            x, y = pt
            if not (0 <= x <= width and 0 <= y <= height):
                raise HTTPException(
                    422,
                    f"Zone {zone.zone_id} point {pt_idx} ({x},{y}) outside frame {width}x{height}",
                )

    zones_data = [{
        "zone_id": z.zone_id,
        "name": z.name,
        "polygon": z.polygon,
    } for z in req.zones]

    output = {
        "camera_id": camera_id,
        "coordinate_space": "original_frame",
        "frame_size": {"width": width, "height": height},
        "zones": zones_data,
    }

    zones_path = _get_zones_path(camera_id)
    _write_zones_yaml_atomic(zones_path, output)

    logger.info("Zones updated for %s: %d zones", camera_id, len(zones_data))
    return {"status": "saved", "camera_id": camera_id, "zone_count": len(zones_data)}
