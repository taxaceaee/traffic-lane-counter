"""Camera management API — list, get, create, delete camera configs."""

import logging
import shutil
import struct
import time
import zlib
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from backend.io.safe_path import safe_join, validate_identifier

logger = logging.getLogger("trafficflow.cameras")

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

_CONFIGS_DIR = Path("configs")
_CAMERAS_DIR = _CONFIGS_DIR / "cameras"
_LANES_DIR = _CONFIGS_DIR / "lanes"

# Snapshot cache: camera_id -> (jpeg_bytes, width, height, timestamp)
# Reduces latency on repeated loads (e.g., switching cameras in lane editor).
_snapshot_cache: dict[str, tuple[bytes, int, int, float]] = {}
_SNAPSHOT_CACHE_TTL = 2.0  # seconds


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
        from backend.db.session import SessionLocal
        from backend.db.repositories import SqlQueryRepository

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
        from backend.db.session import SessionLocal
        from backend.db.repositories import SqlQueryRepository

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
    """Return real lane-change events (stable_lane changes) from the DB."""
    validate_identifier(camera_id, name="camera_id")
    try:
        from backend.db.session import SessionLocal
        from backend.db.repositories import SqlLaneChangeRepository

        session = SessionLocal()
        try:
            repo = SqlLaneChangeRepository(session)
            return repo.get_events(camera_id, limit=limit)
        finally:
            session.close()
    except Exception:
        logger.error("DB query failed for lane-changes/%s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema")


def _get_snapshot_from_pipeline(camera_id: str) -> tuple[bytes | None, int | None, int | None]:
    """Try to grab a frame from an already-running live pipeline.

    Avoids the overhead of opening a new cv2.VideoCapture() by reusing the
    shared ``_streams`` dict in ``routes_live``. Returns (jpeg, w, h) or
    (None, None, None) if no pipeline is active.
    """
    try:
        from backend.api.routes_live import _streams as live_streams, _last_full_frames
        if camera_id not in live_streams:
            return None, None, None
        # Grab the latest full pipeline frame (pre-ROI-crop) from capture loop
        last_full = _last_full_frames.get(camera_id)
        if last_full is None or last_full.size == 0:
            return None, None, None
        h, w = last_full.shape[:2]
        success, jpeg_buf = cv2.imencode(".jpg", last_full, [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not success:
            return None, None, None
        return jpeg_buf.tobytes(), w, h
    except Exception:
        logger.debug("Failed to grab snapshot from pipeline", exc_info=True)
        return None, None, None


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
            success, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
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
        success, jpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
        if not success:
            raise RuntimeError("JPEG encoding failed")

        return jpeg_buf.tobytes(), w, h

    finally:
        if cap is not None:
            cap.release()


def _make_placeholder(camera_id: str, reason: str = "") -> bytes:
    """Generate a placeholder JPEG with camera ID, grid dots, and error reason.

    Returns JPEG bytes so the lane config canvas always has a background
    to draw on, even when the real snapshot is unavailable.
    """
    w, h = 1280, 720
    img = np.full((h, w, 3), (15, 23, 42), dtype=np.uint8)  # dark slate

    # Grid dots
    cv2.putText(img, "", (0, 0), cv2.FONT_HERSHEY_SIMPLEX, 1, (30, 41, 59), 1)  # seed
    for x in range(0, w, 80):
        for y in range(0, h, 80):
            cv2.circle(img, (x, y), 2, (30, 41, 59), -1)

    # Camera ID — centered header
    text = f"Camera: {camera_id}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale, thick = 1.2, 2
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    cx = (w - tw) // 2
    cv2.putText(img, text, (cx, h // 2 - 40), font, scale, (148, 163, 184), thick)

    # Subtitle
    sub = "No snapshot available — lane config canvas"
    (sw, sh), _ = cv2.getTextSize(sub, font, 0.7, 1)
    cv2.putText(img, sub, ((w - sw) // 2, h // 2 + 10), font, 0.7, (100, 116, 139), 1)

    if reason:
        # Wrap reason text to fit width
        max_w = w - 80
        words = reason.split()
        lines = []
        cur = ""
        for word in words:
            test = f"{cur} {word}".strip()
            (tw, _), _ = cv2.getTextSize(test, font, 0.45, 1)
            if tw > max_w and cur:
                lines.append(cur)
                cur = word
            else:
                cur = test
        if cur:
            lines.append(cur)
        for i, line in enumerate(lines[-4:]):  # show at most 4 lines
            y_off = h // 2 + 50 + i * 22
            cv2.putText(img, line, (40, y_off), font, 0.45, (71, 85, 105), 1)

    success, jpeg_buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return jpeg_buf.tobytes()


@router.get("/{camera_id}/snapshot")
async def camera_snapshot(camera_id: str):
    """Return a single JPEG frame from the camera source.

    Optimization layers (from fastest to slowest):
      1. Server-side cache (TTL 2s) — repeated loads return instantly
      2. Live pipeline reuse — grabs frame from already-running stream
      3. Open & close — fallback when no pipeline is active
    """
    validate_identifier(camera_id, name="camera_id")

    # 1. Check server-side cache
    now = time.monotonic()
    cached = _snapshot_cache.get(camera_id)
    if cached is not None and (now - cached[3]) < _SNAPSHOT_CACHE_TTL:
        return Response(content=cached[0], media_type="image/jpeg",
                        headers={"X-Frame-Width": str(cached[1]), "X-Frame-Height": str(cached[2])})

    # 2. Try to grab from live pipeline (no new capture overhead)
    pip_jpeg, pip_w, pip_h = _get_snapshot_from_pipeline(camera_id)
    if pip_jpeg is not None:
        _snapshot_cache[camera_id] = (pip_jpeg, pip_w, pip_h, now)
        return Response(content=pip_jpeg, media_type="image/jpeg",
                        headers={"X-Frame-Width": str(pip_w), "X-Frame-Height": str(pip_h)})

    # 3. Fallback: open source, grab one frame, close
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
        logger.warning("Snapshot failed for camera %s: %s — using placeholder", camera_id, e)
        # Generate placeholder image so lane config canvas always has a background
        placeholder = _make_placeholder(camera_id, f"{e!s:.120}")
        return Response(content=placeholder, media_type="image/jpeg",
                        headers={"X-Frame-Width": "1280", "X-Frame-Height": "720", "X-Placeholder": "true"})

    # Populate cache
    _snapshot_cache[camera_id] = (jpeg_bytes, w, h, now)
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
        from backend.api.routes_live import _cleanup_stream
        _cleanup_stream(camera_id)
    except Exception:
        pass

    cam_path.unlink()
    # Clear snapshot cache
    _snapshot_cache.pop(camera_id, None)
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
