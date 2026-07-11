"""Camera management API — list, get, create, delete camera configs."""

import logging
import os
import shutil
import struct
import time
import zlib
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from tf_api.api.routes_auth import get_current_user, require_admin
from tf_common.safe_path import safe_join, validate_identifier

logger = logging.getLogger("trafficflow.cameras")

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

_CONFIGS_DIR = Path("configs")
_CAMERAS_DIR = _CONFIGS_DIR / "cameras"
_LANES_DIR = _CONFIGS_DIR / "lanes"
_ZONES_DIR = _CONFIGS_DIR / "detection_zones"

_YOUTUBE_CAMERA_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "camera_id": "YT_LIVE_TEST",
        "name": "YouTube Live Camera 01",
        "source_type": "youtube_live",
        "source": "https://www.youtube.com/watch?v=sJvEFrG0wq0",
        "fps": 30.0,
        "frame_width": 1280,
        "frame_height": 720,
    },
    {
        "camera_id": "YT_LIVE_TEST_02",
        "name": "YouTube Live Camera 02",
        "source_type": "youtube_live",
        "source": "https://www.youtube.com/watch?v=1EamsYw_Xyo",
        "fps": 30.0,
        "frame_width": 1280,
        "frame_height": 720,
    },
    {
        "camera_id": "YT_LIVE_TEST_03",
        "name": "YouTube Live Camera 03",
        "source_type": "youtube_live",
        "source": "https://www.youtube.com/watch?v=G_G8A6JU_LI",
        "fps": 30.0,
        "frame_width": 1280,
        "frame_height": 720,
    },
)
_YOUTUBE_CAMERA_IDS = {item["camera_id"] for item in _YOUTUBE_CAMERA_PRESETS}
_YOUTUBE_CAMERA_SOURCES = {item["source"] for item in _YOUTUBE_CAMERA_PRESETS}

# Snapshot cache: camera_id -> (jpeg_bytes, width, height, timestamp)
_snapshot_cache: dict[str, tuple[bytes, int, int, float]] = {}
_SNAPSHOT_CACHE_TTL = 2.0


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


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def _ensure_camera_support_files(camera_id: str, frame_width: int, frame_height: int) -> None:
    lanes_path = _LANES_DIR / f"{camera_id}_lanes.yaml"
    if not lanes_path.exists():
        _write_yaml(
            lanes_path,
            {
                "camera_id": camera_id,
                "coordinate_space": "original_frame",
                "frame_size": {"width": frame_width, "height": frame_height},
                "lanes": [],
            },
        )

    zones_path = _ZONES_DIR / f"{camera_id}_zones.yaml"
    if not zones_path.exists():
        _write_yaml(
            zones_path,
            {
                "camera_id": camera_id,
                "coordinate_space": "original_frame",
                "frame_size": {"width": frame_width, "height": frame_height},
                "zones": [],
            },
        )


def _camera_config_payload(
    *,
    camera_id: str,
    name: str,
    source_type: str,
    source: str,
    fps: float,
    frame_width: int,
    frame_height: int,
    model_id: str,
) -> dict[str, Any]:
    return {
        "camera": {
            "camera_id": camera_id,
            "name": name,
            "source_type": source_type,
            "source": source,
            "fps": fps,
            "frame_size": {"width": frame_width, "height": frame_height},
            "allow_scaling": True,
        },
        "model": {
            "model_id": model_id,
            "conf_threshold": 0.35,
            "iou_threshold": 0.5,
            "imgsz": 640,
            "allowed_classes": ["car", "motorcycle", "truck", "bus"],
        },
        "tracking": {
            "tracker": "bytetrack.yaml",
            "active_track_timeout_frames": 10,
            "min_track_age_frames": 3,
        },
        "lanes": {
            "config_path": f"../lanes/{camera_id}_lanes.yaml",
        },
        "counting": {
            "min_cross_distance_px": 2.0,
        },
        "output": {
            "save_video": False,
            "save_jsonl": False,
            "save_csv": False,
        },
    }


def _ensure_youtube_presets() -> None:
    for preset in _YOUTUBE_CAMERA_PRESETS:
        camera_id = preset["camera_id"]
        cam_path = safe_join(_CAMERAS_DIR, f"{camera_id}.yaml")
        data = _camera_config_payload(
            camera_id=camera_id,
            name=preset["name"],
            source_type=preset["source_type"],
            source=preset["source"],
            fps=preset["fps"],
            frame_width=preset["frame_width"],
            frame_height=preset["frame_height"],
            model_id="yolo11n_coco",
        )
        if cam_path.exists():
            current = _load_camera_config(camera_id) or {}
            needs_rewrite = (
                current.get("source") != preset["source"]
                or current.get("source_type") != "youtube_live"
                or current.get("frame_size", {}).get("width") != preset["frame_width"]
                or current.get("frame_size", {}).get("height") != preset["frame_height"]
                or str(current.get("lanes", {}).get("config_path", "")).endswith("CAM_01_lanes.yaml")
            )
            if needs_rewrite:
                _write_yaml(cam_path, data)
        else:
            _write_yaml(cam_path, data)
        _ensure_camera_support_files(camera_id, preset["frame_width"], preset["frame_height"])


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
    _ensure_youtube_presets()
    result = []
    for cid in _list_camera_ids():
        if cid == "CAM_01":
            continue
        cfg = _load_camera_config(cid)
        if cfg:
            result.append(_build_camera_summary(cid, cfg))
    return result


@router.get("")
async def list_cameras(_user: dict = Depends(get_current_user)):
    """Return all registered cameras."""
    return get_camera_list()


@router.get("/{camera_id}")
async def get_camera(camera_id: str, _user: dict = Depends(get_current_user)):
    validate_identifier(camera_id, name="camera_id")
    cfg = _load_camera_config(camera_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Camera not found: {camera_id}")
    return _build_camera_summary(camera_id, cfg)


@router.get("/{camera_id}/occupancy/latest")
async def get_latest_occupancy(camera_id: str, _user: dict = Depends(get_current_user)):
    validate_identifier(camera_id, name="camera_id")
    try:
        from tf_db.repositories import SqlQueryRepository
        from tf_db.session import SessionLocal

        session = SessionLocal()
        try:
            repo = SqlQueryRepository(session)
            occ = repo.get_latest_occupancy(camera_id)
            return {"camera_id": camera_id, "occupancy": occ}
        finally:
            session.close()
    except Exception:
        logger.error("DB query failed for occupancy/latest/%s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema") from None


@router.get("/{camera_id}/occupancy")
async def get_occupancy_history(
    camera_id: str,
    limit: int = 500,
    _user: dict = Depends(get_current_user),
):
    validate_identifier(camera_id, name="camera_id")
    try:
        from tf_db.repositories import SqlQueryRepository
        from tf_db.session import SessionLocal

        session = SessionLocal()
        try:
            repo = SqlQueryRepository(session)
            return repo.get_occupancy_history(camera_id, limit=limit)
        finally:
            session.close()
    except Exception:
        logger.error("DB query failed for occupancy/%s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema") from None


@router.get("/{camera_id}/lane-changes")
async def get_lane_changes(
    camera_id: str,
    limit: int = 50,
    _user: dict = Depends(get_current_user),
):
    validate_identifier(camera_id, name="camera_id")
    try:
        from tf_db.repositories import SqlQueryRepository
        from tf_db.session import SessionLocal

        session = SessionLocal()
        try:
            repo = SqlQueryRepository(session)
            return repo.get_lane_changes(camera_id, limit=limit)
        finally:
            session.close()
    except Exception:
        logger.error("DB query failed for lane-changes/%s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema") from None


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
            from tf_common.yt_utils import resolve_stream_url
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


def _get_snapshot_from_pipeline(camera_id: str) -> tuple[bytes | None, int | None, int | None]:
    try:
        from tf_api.api.routes_live import _last_snapshots, _streams
        if camera_id not in _streams:
            return None, None, None
        cached = _last_snapshots.get(camera_id)
        if cached is None:
            return None, None, None
        jpeg_bytes, width, height, _ts = cached
        return jpeg_bytes, width, height
    except Exception:
        logger.debug("Failed to reuse pipeline snapshot", exc_info=True)
        return None, None, None


def _make_placeholder(camera_id: str, reason: str = "") -> bytes:
    w, h = 1280, 720
    img = np.full((h, w, 3), (15, 23, 42), dtype=np.uint8)

    for x in range(0, w, 80):
        for y in range(0, h, 80):
            cv2.circle(img, (x, y), 2, (30, 41, 59), -1)

    header = f"Camera: {camera_id}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 1.2
    thick = 2
    (tw, _), _ = cv2.getTextSize(header, font, scale, thick)
    cv2.putText(img, header, ((w - tw) // 2, h // 2 - 40), font, scale, (148, 163, 184), thick)

    subtitle = "No snapshot available - lane config canvas"
    (sw, _), _ = cv2.getTextSize(subtitle, font, 0.7, 1)
    cv2.putText(img, subtitle, ((w - sw) // 2, h // 2 + 10), font, 0.7, (100, 116, 139), 1)

    if reason:
        max_w = w - 80
        words = reason.split()
        lines: list[str] = []
        cur = ""
        for word in words:
            test = f"{cur} {word}".strip()
            (line_w, _), _ = cv2.getTextSize(test, font, 0.45, 1)
            if line_w > max_w and cur:
                lines.append(cur)
                cur = word
            else:
                cur = test
        if cur:
            lines.append(cur)
        for idx, line in enumerate(lines[-4:]):
            cv2.putText(img, line, (40, h // 2 + 50 + idx * 22), font, 0.45, (71, 85, 105), 1)

    success, jpeg_buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if success:
        return jpeg_buf.tobytes()

    raw = img.tobytes()
    png = b""
    png += b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack("!2I5B", w, h, 8, 2, 0, 0, 0)
    png += struct.pack("!I", len(ihdr)) + b"IHDR" + ihdr + struct.pack("!I", zlib.crc32(b"IHDR" + ihdr))
    row_stride = w * 3
    filtered = b"".join(b"\x00" + raw[i:i + row_stride] for i in range(0, len(raw), row_stride))
    comp = zlib.compress(filtered, level=6)
    png += struct.pack("!I", len(comp)) + b"IDAT" + comp + struct.pack("!I", zlib.crc32(b"IDAT" + comp))
    png += struct.pack("!I", 0) + b"IEND" + b"" + struct.pack("!I", zlib.crc32(b"IEND"))
    return png


@router.get("/{camera_id}/snapshot")
async def camera_snapshot(camera_id: str, _user: dict = Depends(get_current_user)):
    """Return a single JPEG frame from the camera source.

    Lightweight — no AI pipeline, no persistent stream.
    Opens the source, grabs one frame, closes immediately.
    """
    validate_identifier(camera_id, name="camera_id")
    now = time.monotonic()
    cached = _snapshot_cache.get(camera_id)
    if cached is not None and (now - cached[3]) < _SNAPSHOT_CACHE_TTL:
        return Response(
            content=cached[0],
            media_type="image/jpeg",
            headers={"X-Frame-Width": str(cached[1]), "X-Frame-Height": str(cached[2])},
        )

    pip_jpeg, pip_w, pip_h = _get_snapshot_from_pipeline(camera_id)
    if pip_jpeg is not None and pip_w is not None and pip_h is not None:
        _snapshot_cache[camera_id] = (pip_jpeg, pip_w, pip_h, now)
        return Response(
            content=pip_jpeg,
            media_type="image/jpeg",
            headers={"X-Frame-Width": str(pip_w), "X-Frame-Height": str(pip_h)},
        )

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
        placeholder = _make_placeholder(camera_id, f"{e!s:.120}")
        return Response(
            content=placeholder,
            media_type="image/jpeg",
            headers={"X-Frame-Width": "1280", "X-Frame-Height": "720", "X-Placeholder": "true"},
        )

    _snapshot_cache[camera_id] = (jpeg_bytes, w, h, now)
    return Response(
        content=jpeg_bytes,
        media_type="image/jpeg",
        headers={"X-Frame-Width": str(w), "X-Frame-Height": str(h)},
    )


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
async def create_camera(req: CameraCreateRequest, _user: dict = Depends(require_admin)):
    """Register a new camera by creating its YAML config file."""
    validate_identifier(req.camera_id, name="camera_id")
    cam_path = safe_join(_CAMERAS_DIR, f"{req.camera_id}.yaml")
    if cam_path.exists():
        raise HTTPException(409, f"Camera already exists: {req.camera_id}")

    normalized_source_type = req.source_type
    if normalized_source_type == "youtube":
        normalized_source_type = "youtube_live"

    if normalized_source_type == "youtube_live" and req.source not in _YOUTUBE_CAMERA_SOURCES:
        raise HTTPException(422, "Only the approved YouTube camera URLs are allowed for youtube_live sources")

    config = _camera_config_payload(
        camera_id=req.camera_id,
        name=req.name,
        source_type=normalized_source_type,
        source=req.source,
        fps=req.fps,
        frame_width=req.frame_width,
        frame_height=req.frame_height,
        model_id=req.model_id,
    )

    _write_yaml(cam_path, config)
    _ensure_camera_support_files(req.camera_id, req.frame_width, req.frame_height)

    cfg = _load_camera_config(req.camera_id)
    logger.info("Camera created: %s", req.camera_id)
    return _build_camera_summary(req.camera_id, cfg)


@router.delete("/{camera_id}")
async def delete_camera(camera_id: str, _user: dict = Depends(require_admin)):
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
        logger.debug("Live pipeline cleanup failed for %s", camera_id, exc_info=True)

    cam_path.unlink()
    # Remove lanes file
    lanes_path = _LANES_DIR / f"{camera_id}_lanes.yaml"
    if lanes_path.exists():
        lanes_path.unlink()
    zones_path = _ZONES_DIR / f"{camera_id}_zones.yaml"
    if zones_path.exists():
        zones_path.unlink()
    # Remove lanes lock file
    lock_path = _LANES_DIR / f"{camera_id}_lanes.yaml.lock"
    if lock_path.exists():
        lock_path.unlink()
    zones_lock_path = _ZONES_DIR / f"{camera_id}_zones.yaml.lock"
    if zones_lock_path.exists():
        zones_lock_path.unlink()
    # Remove storage directory
    storage_dir = Path(os.getenv("STORAGE_ROOT", "data/storage")) / camera_id
    if storage_dir.exists():
        shutil.rmtree(storage_dir, ignore_errors=True)
    _snapshot_cache.pop(camera_id, None)

    logger.info("Camera deleted: %s", camera_id)
    return {"status": "deleted", "camera_id": camera_id}
