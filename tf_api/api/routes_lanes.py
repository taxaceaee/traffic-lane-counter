"""Lane config API — get/update lane polygons and counting lines per camera.

Thread-safe: uses file-level locking to prevent concurrent writes.
"""

import contextlib
import fcntl
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from tf_api.api.routes_auth import get_current_user, require_operator
from tf_common.safe_path import safe_join, validate_identifier

logger = logging.getLogger("trafficflow.lanes")

router = APIRouter(prefix="/api/cameras", tags=["lanes"])

_CONFIGS_DIR = Path("configs")
_LANES_DIR = _CONFIGS_DIR / "lanes"


class CountingLineDef(BaseModel):
    start: list[float] = Field(..., min_length=2, max_length=2)
    end: list[float] = Field(..., min_length=2, max_length=2)
    direction_ref: list[float] = Field(..., min_length=2, max_length=2)


class LaneUpdateItem(BaseModel):
    lane_id: str
    name: str = ""
    polygon: list[list[float]]
    counting_line: CountingLineDef | None = None


class LaneUpdateRequest(BaseModel):
    lanes: list[LaneUpdateItem]


def _get_lanes_path(camera_id: str) -> Path:
    validate_identifier(camera_id, name="camera_id")
    return safe_join(_LANES_DIR, f"{camera_id}_lanes.yaml")


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


def _read_lanes_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_lanes_yaml_atomic(path: Path, data: dict) -> None:
    """Write lane YAML with atomic file replacement using a unique temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".yaml.lock")
    lock_fh = open(lock_path, "w")  # noqa: SIM115 - fcntl lock lifetime spans the write
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
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()
        with contextlib.suppress(OSError):
            tmp.unlink()


def _acquire_lock(path: Path) -> Any:
    """Acquire an exclusive lock on the lanes file. Returns the lock file handle."""
    lock_path = path.with_suffix(".yaml.lock")
    lock_fh = open(lock_path, "w")  # noqa: SIM115 - caller owns the lock handle
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    return lock_fh


def _release_lock(lock_fh: Any) -> None:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()


@router.get("/{camera_id}/lanes")
async def get_lanes(camera_id: str, _user: dict = Depends(get_current_user)):
    """Get lane configuration for a camera — includes polygons and counting lines."""
    validate_identifier(camera_id, name="camera_id")

    lanes_path = _get_lanes_path(camera_id)
    if not lanes_path.exists():
        return []

    with open(lanes_path, encoding="utf-8") as f:  # noqa: ASYNC230 - small local YAML file
        raw = yaml.safe_load(f) or {}

    lanes = []
    for item in raw.get("lanes", []):
        lane = {
            "lane_id": item.get("lane_id", ""),
            "name": item.get("name", ""),
            "polygon": item.get("polygon", []),
        }
        cl = item.get("counting_line")
        if cl and isinstance(cl, dict):
            lane["counting_line"] = {
                "start": cl.get("start", [0, 0]),
                "end": cl.get("end", [0, 0]),
                "direction_ref": cl.get("direction_ref", [0, 0]),
            }
        lanes.append(lane)
    return lanes


@router.put("/{camera_id}/lanes")
async def update_lanes(
    camera_id: str,
    req: LaneUpdateRequest,
    _user: dict = Depends(require_operator),
):
    """Update lane configuration for a camera. Saves to YAML file atomically with
    file-level locking to prevent concurrent-write corruption."""
    validate_identifier(camera_id, name="camera_id")
    width, height = _get_frame_size(camera_id)

    # Validate all lanes before any write
    lane_ids = set()
    for idx, lane in enumerate(req.lanes):
        validate_identifier(lane.lane_id, name=f"lane_id[{idx}]")
        if lane.lane_id in lane_ids:
            raise HTTPException(422, f"Duplicate lane_id: {lane.lane_id}")
        lane_ids.add(lane.lane_id)

        if len(lane.polygon) < 3:
            raise HTTPException(422, f"Lane {lane.lane_id} polygon must have >= 3 points")

        for pt_idx, pt in enumerate(lane.polygon):
            if len(pt) != 2:
                raise HTTPException(422, f"Lane {lane.lane_id} point {pt_idx} must be [x, y]")
            x, y = pt
            if not (0 <= x <= width and 0 <= y <= height):
                raise HTTPException(
                    422,
                    f"Lane {lane.lane_id} point {pt_idx} ({x},{y}) outside frame {width}x{height}",
                )

        if lane.counting_line:
            for field_name in ("start", "end", "direction_ref"):
                pt = getattr(lane.counting_line, field_name)
                if len(pt) != 2:
                    raise HTTPException(
                        422, f"Lane {lane.lane_id} counting_line.{field_name} must be [x, y]"
                    )
                x, y = pt
                if not (0 <= x <= width and 0 <= y <= height):
                    raise HTTPException(
                        422,
                        f"Lane {lane.lane_id} counting_line.{field_name} ({x},{y}) outside frame {width}x{height}",
                    )

    lanes_data = []
    for lane in req.lanes:
        entry: dict[str, Any] = {
            "lane_id": lane.lane_id,
            "name": lane.name,
            "polygon": lane.polygon,
        }
        if lane.counting_line:
            entry["counting_line"] = {
                "start": lane.counting_line.start,
                "end": lane.counting_line.end,
                "direction_ref": lane.counting_line.direction_ref,
            }
        lanes_data.append(entry)

    output = {
        "camera_id": camera_id,
        "coordinate_space": "original_frame",
        "frame_size": {"width": width, "height": height},
        "lanes": lanes_data,
    }

    # Atomic write with file locking
    lanes_path = _get_lanes_path(camera_id)
    _write_lanes_yaml_atomic(lanes_path, output)

    # Request a config-only hot reload. The existing video reader stays alive;
    # the capture loop swaps DetectionCore/ROI at the next frame boundary.
    from tf_api.api.routes_live import _request_stream_reload
    live_reloaded = _request_stream_reload(camera_id)

    logger.info("Lanes updated for %s: %d lanes", camera_id, len(lanes_data))
    return {
        "status": "saved",
        "camera_id": camera_id,
        "lane_count": len(lanes_data),
        "live_reloaded": live_reloaded,
    }
