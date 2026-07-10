"""Stream management API — start/stop/list continuous camera detection streams.

Continuous streams run in background threads: read frames → DetectionCore
→ push results to configured callbacks (HTTP backend, ring buffer).
"""

import json
import logging
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from detection_server.services.manager import StreamManager
from detection_server.services.callbacks import RingBufferCallback

logger = logging.getLogger("detection_server.stream_api")

router = APIRouter(prefix="/stream", tags=["streams"])

# Global manager + ring buffer (shared with main.py)
manager = StreamManager()
ring_buffer = RingBufferCallback(maxlen=100)


# ── Request schemas ────────────────────────────────────────────────────────

class CameraSourceConfig(BaseModel):
    """Camera source configuration from YAML."""
    camera_id: str = Field(..., description="Unique camera identifier")
    name: str = ""
    source_type: str = Field(default="rtsp", description="video, rtsp, youtube, youtube_live, image_dir")
    source: str = Field(..., description="Video source URL or path")
    fps: float = Field(default=25.0, ge=1.0, le=60.0)
    frame_width: int = Field(default=1280, ge=1)
    frame_height: int = Field(default=720, ge=1)
    model_id: str = Field(default="yolo11s")
    conf_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    iou_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    half: bool = False
    allowed_classes: list[str] = Field(default=["car", "motorcycle", "truck", "bus"])
    detect_every_n_frames: int = Field(default=2, ge=1)
    min_track_age_frames: int = Field(default=5, ge=1)
    min_cross_distance_px: float = Field(default=2.0, ge=0.0)
    reconnect: bool = True
    push_endpoint: str = ""


class CameraSourceFromYAML(BaseModel):
    """Load camera config from YAML file path on the server."""
    camera_id: str = Field(..., description="Camera to start")
    yaml_path: str = ""
    push_endpoint: str = ""


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/start")
async def start_stream(cfg: CameraSourceConfig):
    """Start continuous detection for a camera from inline config.

    Config includes lanes from YAML? No — lanes are loaded from
    configs/lanes/{camera_id}_lanes.yaml by reading the file on disk.

    The detection server reads the camera YAML (for source info) and
    lanes YAML (for polygons) directly from its filesystem.
    """
    config = _build_pipeline_config(cfg)

    if manager.list_streams().count(cfg.camera_id):
        manager.stop_stream(cfg.camera_id)

    push_cb = _build_push_callback(cfg.push_endpoint)

    result = manager.start_stream(
        camera_id=cfg.camera_id,
        source=cfg.source,
        source_type=cfg.source_type,
        config=config,
        zone_polygons=None,  # loaded from detection_zones/ if exists
        push_callback=push_cb,
        fps=cfg.fps,
        reconnect=cfg.reconnect,
    )
    return result


@router.post("/start-from-yaml")
async def start_from_yaml(cfg: CameraSourceFromYAML):
    """Start continuous detection from a YAML camera config file."""
    yaml_path = cfg.yaml_path or f"configs/cameras/{cfg.camera_id}.yaml"

    try:
        with open(yaml_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError) as e:
        raise HTTPException(400, f"Cannot load camera config: {e}")

    cam_section = raw.get("camera", raw)
    model_section = raw.get("model", {})
    tracking_section = raw.get("tracker", {})
    occ_section = raw.get("occupancy", {})

    frame = cam_section.get("frame_size", {})
    req = CameraSourceConfig(
        camera_id=cam_section.get("camera_id", cfg.camera_id),
        name=cam_section.get("name", ""),
        source_type=cam_section.get("source_type", "video"),
        source=cam_section.get("source", ""),
        fps=cam_section.get("fps", 25),
        frame_width=frame.get("width", 960),
        frame_height=frame.get("height", 540),
        model_id=model_section.get("model_id", "yolo11s"),
        conf_threshold=model_section.get("conf_threshold", 0.35),
        iou_threshold=model_section.get("iou_threshold", 0.5),
        half=bool(model_section.get("half", False)),
        allowed_classes=model_section.get("allowed_classes", ["car", "motorcycle", "truck", "bus"]),
        detect_every_n_frames=tracking_section.get("detect_every_n_frames", 2),
        min_track_age_frames=tracking_section.get("min_track_age_frames", 5),
        min_cross_distance_px=occ_section.get("min_cross_distance_px", 2.0),
        reconnect=cfg.yaml_path == "",  # auto-reconnect for live sources
        push_endpoint=cfg.push_endpoint,
    )
    return await start_stream(req)


@router.post("/stop/{camera_id}")
async def stop_stream(camera_id: str):
    """Stop continuous detection for a camera."""
    result = manager.stop_stream(camera_id)
    ring_buffer.clear(camera_id)
    return result


@router.get("/list")
async def list_streams():
    """List all running camera streams."""
    streams = manager.list_streams()
    result = {"count": len(streams), "cameras": []}
    for cid in streams:
        stats = manager.get_stats(cid)
        result["cameras"].append({
            "camera_id": cid,
            "status": stats.get("status"),
            "frame_idx": stats.get("frame_idx", 0),
            "fps": round(stats.get("fps", 0), 1),
            "track_count": stats.get("track_count", 0),
            "occupancy": stats.get("occupancy", {}),
        })
    return result


@router.get("/stats/{camera_id}")
async def get_stream_stats(camera_id: str):
    """Get detailed stats for a camera stream."""
    stats = manager.get_stats(camera_id)
    if "error" in stats:
        raise HTTPException(404, stats["error"])
    return stats


@router.get("/output/{camera_id}")
async def get_stream_output(camera_id: str, count: int = Query(default=10, ge=1, le=200)):
    """Get the last N detection results from the ring buffer for a camera."""
    results = ring_buffer.get_latest(camera_id, count)
    return {"camera_id": camera_id, "count": len(results), "results": results}


@router.post("/restart/{camera_id}")
async def restart_stream(camera_id: str):
    """Restart a running stream (e.g., after config change)."""
    return manager.restart_stream(camera_id)


@router.post("/stop-all")
async def stop_all_streams():
    """Stop all running streams."""
    manager.stop_all(timeout=15.0)
    return {"status": "all_stopped"}


# ── Helpers ──────────────────────────────────────────────────────────────

def _build_pipeline_config(cfg: CameraSourceConfig) -> dict:
    """Convert CameraSourceConfig to DetectionCore pipeline config."""
    camera_id = cfg.camera_id
    lanes = _load_lanes(camera_id, cfg.frame_width, cfg.frame_height)
    zones = _load_zones(camera_id)

    config = {
        "camera_id": camera_id,
        "_lanes_path": str(Path(f"configs/lanes/{camera_id}_lanes.yaml")),
        "_zones_path": str(Path(f"configs/detection_zones/{camera_id}_zones.yaml")),
        "frame_size": {"width": cfg.frame_width, "height": cfg.frame_height},
        "coordinate_space": "original_frame",
        "detector": {
            "weights": cfg.model_id,
            "imgsz": 640,
            "conf": cfg.conf_threshold,
            "iou": cfg.iou_threshold,
            "half": cfg.half,
            "allowed_classes": cfg.allowed_classes,
            "detect_every_n_frames": cfg.detect_every_n_frames,
        },
        "tracking": {
            "tracker": "bytetrack.yaml",
            "min_track_age_frames": cfg.min_track_age_frames,
        },
        "occupancy": {
            "history_window": 10,
            "min_consecutive_for_change": 5,
            "unknown_timeout_frames": 15,
        },
        "counting": {
            "min_cross_distance_px": cfg.min_cross_distance_px,
        },
        "lanes": lanes,
        "zone_polygons": zones,
    }
    return config


def _load_lanes(camera_id: str, width: int = 960, height: int = 540) -> list:
    """Load lane polygons from configs/lanes/{camera_id}_lanes.yaml."""
    lanes_path = Path(f"configs/lanes/{camera_id}_lanes.yaml")
    if not lanes_path.exists():
        return []

    with open(lanes_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    result = []
    for item in raw.get("lanes", []):
        entry = {
            "lane_id": item["lane_id"],
            "name": item.get("name", ""),
            "polygon": item["polygon"],
        }
        if "counting_line" in item:
            entry["counting_line"] = item["counting_line"]

        # Convert to DetectionCore format (uses "id" and "points")
        result.append({
            "id": item["lane_id"],
            "points": item["polygon"],
            **(item["counting_line"] if "counting_line" in item else {}),
        })
    return result


def _load_zones(camera_id: str) -> list | None:
    """Load detection zone polygons from configs/detection_zones/{camera_id}_zones.yaml."""
    zones_path = Path(f"configs/detection_zones/{camera_id}_zones.yaml")
    if not zones_path.exists():
        return None

    with open(zones_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return [z["polygon"] for z in raw.get("zones", [])] or None


def _build_push_callback(endpoint: str):
    """Create a push callback chain: ring_buffer + optional HTTP."""
    callbacks = [ring_buffer]

    if endpoint:
        from detection_server.services.callbacks import HTTPCallback
        callbacks.append(HTTPCallback(endpoint))

    if len(callbacks) == 1:
        return callbacks[0]

    # Chain multiple callbacks
    def chain(camera_id: str, result: dict) -> None:
        for cb in callbacks:
            try:
                cb(camera_id, result)
            except Exception:
                logger.exception("Callback failed for %s", camera_id)

    return chain
