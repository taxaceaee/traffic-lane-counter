"""Camera management API — list, get, create, delete camera configs."""

import logging
import shutil
import time
from pathlib import Path
from typing import Any

import cv2
import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from tf_common.safe_path import safe_join, validate_identifier

logger = logging.getLogger("trafficflow.cameras")

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

_CONFIGS_DIR = Path("configs")
_CAMERAS_DIR = _CONFIGS_DIR / "cameras"
_LANES_DIR = _CONFIGS_DIR / "lanes"


def _load_camera_config(camera_id: str) -> dict[str, Any] | None:
    validate_identifier(camera_id, name="camera_id")
    path = safe_join(_CAMERAS_DIR, f"{camera_id}.yaml")
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = {**raw}
    camera_section = cfg.pop("camera", {})
    if isinstance(camera_section, dict):
        for k, v in camera_section.items():
            cfg.setdefault(k, v)
    return cfg


def _list_camera_ids() -> list[str]:
    if not _CAMERAS_DIR.exists():
        return []
    return sorted(p.stem for p in _CAMERAS_DIR.glob("*.yaml"))


def _build_camera_summary(camera_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    frame = cfg.get("frame_size", {})
    return {
        "camera_id": camera_id,
        "name": cfg.get("name", ""),
        "source": cfg.get("source", ""),
        "source_type": cfg.get("source_type", "video"),
        "fps": cfg.get("fps", 25.0),
        "frame_width": frame.get("width", 960),
        "frame_height": frame.get("height", 540),
        "status": "configured",
        "model_id": cfg.get("model", {}).get("model_id", ""),
    }


def get_camera_list() -> list[dict[str, Any]]:
    """Synchronous version of list_cameras for use from background threads."""
    result = []
    for cid in _list_camera_ids():
        cfg = _load_camera_config(cid)
        if cfg:
            result.append(_build_camera_summary(cid, cfg))
    return result


@router.get("")
async def list_cameras():
    """Return all registered cameras."""
    return get_camera_list()


@router.get("/{camera_id}")
async def get_camera(camera_id: str):
    validate_identifier(camera_id, name="camera_id")
    cfg = _load_camera_config(camera_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")
    return _build_camera_summary(camera_id, cfg)


@router.get("/{camera_id}/occupancy/latest")
async def get_latest_occupancy(camera_id: str):
    validate_identifier(camera_id, name="camera_id")
    try:
        from tf_db.session import SessionLocal
        from tf_db.repositories import SqlQueryRepository

        session = SessionLocal()
        try:
            repo = SqlQueryRepository(session)
            occ = repo.get_latest_occupancy(camera_id)
            return {"camera_id": camera_id, "occupancy": occ}
        finally:
            session.close()
    except Exception:
        logger.error("DB query failed for occupancy/latest/%s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema")


@router.get("/{camera_id}/occupancy")
async def get_occupancy_history(camera_id: str, limit: int = 500):
    validate_identifier(camera_id, name="camera_id")
    try:
        from tf_db.session import SessionLocal
        from tf_db.repositories import SqlQueryRepository

        session = SessionLocal()
        try:
            repo = SqlQueryRepository(session)
            return repo.get_occupancy_history(camera_id, limit=limit)
        finally:
            session.close()
    except Exception:
        logger.error("DB query failed for occupancy/%s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema")


@router.get("/{camera_id}/lane-changes")
async def get_lane_changes(camera_id: str, limit: int = 50):
    validate_identifier(camera_id, name="camera_id")
    try:
        from tf_db.session import SessionLocal
        from tf_db.repositories import SqlQueryRepository

        session = SessionLocal()
        try:
            repo = SqlQueryRepository(session)
            return repo.get_lane_changes(camera_id, limit=limit)
        finally:
            session.close()
    except Exception:
        logger.error("DB query failed for lane-changes/%s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema")


def _grab_single_frame(source: str, source_type: str, timeout: float = 10.0) -> tuple[bytes, int, int]:
    """Open video source, read one frame, return (jpeg_bytes, width, height).

    Supports video files, image_dir, RTSP streams, and YouTube (live/one-shot).
    Always releases the capture before returning.
    """
    cap = None
    try:
        # ── Image directory: read the first supported image ─────────────
        if source_type == "image_dir":
            img_exts = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}
            src_path = Path(source)
            if not src_path.is_dir():
                raise ValueError(f"Not a directory: {source}")
            first_img = None
            for f in sorted(src_path.iterdir()):
                if f.suffix.lower() in img_exts:
                    first_img = f
                    break
            if first_img is None:
                raise ValueError(f"No images found in {source}")
            frame = cv2.imread(str(first_img))
            if frame is None or frame.size == 0:
                raise ValueError(f"Could not read image: {first_img}")
            h, w = frame.shape[:2]
            success, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not success:
                raise RuntimeError("JPEG encoding failed")
            return jpeg_buf.tobytes(), w, h

        # ── YouTube ─────────────────────────────────────────────────────
        if source_type in ("youtube", "youtube_live"):
            from shared.yt_utils import resolve_stream_url
            url = resolve_stream_url(source)
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        else:
            cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)

        if not cap or not cap.isOpened():
            raise ValueError(f"Could not open video source: {source}")

        # Try to read a valid frame (skip corrupted frames)
        deadline = time.monotonic() + timeout
        frame = None
        while time.monotonic() < deadline:
            ret, f = cap.read()
            if ret and f is not None and f.size > 0:
                frame = f
                break
            time.sleep(0.05)

        if frame is None:
            raise TimeoutError(f"No valid frame after {timeout}s")

        h, w = frame.shape[:2]
        success, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not success:
            raise RuntimeError("JPEG encoding failed")

        return jpeg_buf.tobytes(), w, h

    finally:
        if cap is not None:
            cap.release()


@router.get("/{camera_id}/snapshot")
async def camera_snapshot(camera_id: str):
    """Return a single JPEG frame from the camera source.

    Lightweight — no AI pipeline, no persistent stream.
    Opens the source, grabs one frame, closes immediately.
    """
    validate_identifier(camera_id, name="camera_id")
    cfg = _load_camera_config(camera_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")

    source = cfg.get("source", "")
    source_type = cfg.get("source_type", "video")
    if not source:
        raise HTTPException(status_code=400, detail=f"Camera {camera_id} has no source configured")

    try:
        jpeg_bytes, w, h = _grab_single_frame(source, source_type)
    except Exception as e:
        logger.warning("Snapshot failed for camera %s: %s", camera_id, e)
        raise HTTPException(status_code=503, detail=f"Could not capture frame: {e}")

    return Response(content=jpeg_bytes, media_type="image/jpeg",
                    headers={"X-Frame-Width": str(w), "X-Frame-Height": str(h)})


class CameraCreateRequest(BaseModel):
    camera_id: str = Field(..., min_length=1, description="Unique camera identifier")
    name: str = Field(default="", description="Human-friendly camera name")
    source_type: str = Field(default="video", description="Source type: video, rtsp, youtube, youtube_live")
    source: str = Field(..., min_length=1, description="Video source URL or path")
    fps: float = Field(default=25.0, ge=1.0, le=60.0)
    frame_width: int = Field(default=960, ge=1)
    frame_height: int = Field(default=540, ge=1)
    model_id: str = Field(default="yolo11n_coco", description="Default detection model ID")


@router.post("", status_code=201)
async def create_camera(req: CameraCreateRequest):
    """Register a new camera by creating its YAML config file."""
    validate_identifier(req.camera_id, name="camera_id")
    cam_path = safe_join(_CAMERAS_DIR, f"{req.camera_id}.yaml")
    if cam_path.exists():
        raise HTTPException(409, f"Camera already exists: {req.camera_id}")

    config = {
        "camera": {
            "camera_id": req.camera_id,
            "name": req.name,
            "source_type": req.source_type,
            "source": req.source,
            "fps": req.fps,
            "frame_size": {"width": req.frame_width, "height": req.frame_height},
        },
        "model": {
            "model_id": req.model_id,
            "conf_threshold": 0.35,
            "iou_threshold": 0.5,
            "imgsz": 640,
            "allowed_classes": ["car", "motorcycle", "truck", "bus"],
        },
        "tracker": {
            "type": "bytetrack",
            "tracker": "bytetrack.yaml",
            "track_timeout_frames": 30,
            "min_track_age_frames": 5,
        },
        "lanes": {
            "config_path": f"../lanes/{req.camera_id}_lanes.yaml",
        },
        "occupancy": {
            "history_window": 10,
            "min_consecutive_for_change": 5,
            "unknown_timeout_frames": 15,
        },
        "output": {
            "save_video": True,
            "save_jsonl": True,
            "save_csv": True,
        },
    }

    cam_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cam_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False)

    # Create empty lanes file
    lanes_path = _LANES_DIR / f"{req.camera_id}_lanes.yaml"
    lanes_path.parent.mkdir(parents=True, exist_ok=True)
    if not lanes_path.exists():
        with open(lanes_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "camera_id": req.camera_id,
                "coordinate_space": "original_frame",
                "frame_size": {"width": req.frame_width, "height": req.frame_height},
                "lanes": [],
            }, f, sort_keys=False)

    cfg = _load_camera_config(req.camera_id)
    logger.info("Camera created: %s", req.camera_id)
    return _build_camera_summary(req.camera_id, cfg)


@router.delete("/{camera_id}")
async def delete_camera(camera_id: str):
    """Delete a camera config and its lanes file."""
    validate_identifier(camera_id, name="camera_id")
    cam_path = safe_join(_CAMERAS_DIR, f"{camera_id}.yaml")
    if not cam_path.exists():
        raise HTTPException(404, f"Camera not found: {camera_id}")

    # Cleanup pipeline if running
    try:
        from tf_api.api.routes_live import _cleanup_stream
        _cleanup_stream(camera_id)
    except Exception:
        pass

    cam_path.unlink()
    # Remove lanes file
    lanes_path = _LANES_DIR / f"{camera_id}_lanes.yaml"
    if lanes_path.exists():
        lanes_path.unlink()
    # Remove lanes lock file
    lock_path = _LANES_DIR / f"{camera_id}_lanes.yaml.lock"
    if lock_path.exists():
        lock_path.unlink()
    # Remove storage directory
    storage_dir = Path("storage") / camera_id
    if storage_dir.exists():
        shutil.rmtree(storage_dir, ignore_errors=True)

    logger.info("Camera deleted: %s", camera_id)
    return {"status": "deleted", "camera_id": camera_id}
