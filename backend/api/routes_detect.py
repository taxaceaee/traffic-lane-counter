"""Detection API — REST single-frame + WebSocket streaming."""

import json
import logging
from typing import Any

import cv2
import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from shared.detection_core import DetectionCore
from shared.schemas.detect import (
    DetectConfig,
    DetectResponse,
    SessionInit,
)

logger = logging.getLogger("trafficflow_server.detect")

router = APIRouter(prefix="/detect", tags=["detection"])


# ------------------------------------------------------------------
# Config builder
# ------------------------------------------------------------------

def _detect_config_to_pipeline_dict(dc: DetectConfig) -> dict[str, Any]:
    return {
        "frame_size": {"width": dc.imgsz, "height": dc.imgsz},
        "coordinate_space": "original_frame",
        "detector": {
            "weights": dc.model_weights,
            "imgsz": dc.imgsz,
            "conf": dc.conf_threshold,
            "iou": dc.iou_threshold,
            "class_mode": dc.class_mode,
            "allowed_classes": dc.allowed_classes,
            "half": dc.half,
            "detect_every_n_frames": dc.detect_every_n_frames,
        },
        "class_modes": {dc.class_mode: dc.allowed_classes},
        "tracking": {
            "tracker": dc.tracker_config,
            "min_track_age_frames": dc.min_track_age_frames,
        },
        "counting": {
            "min_cross_distance_px": dc.min_cross_distance_px,
        },
        "lanes": [
            {
                "id": lane.lane_id,
                "points": lane.polygon,
                **(
                    {"counting_line": lane.counting_line.model_dump()}
                    if lane.counting_line
                    else {}
                ),
            }
            for lane in dc.lanes
        ],
    }


# ------------------------------------------------------------------
# Core holder (loaded once, per-model)
# ------------------------------------------------------------------

_core_cache: dict[str, DetectionCore] = {}


def _get_or_create_core(dc: DetectConfig) -> DetectionCore:
    key = dc.model_weights
    if key not in _core_cache:
        config = _detect_config_to_pipeline_dict(dc)
        _core_cache[key] = DetectionCore(config)
        _core_cache[key].start()
        logger.info("Loaded model: %s", dc.model_weights)
    return _core_cache[key]


# ------------------------------------------------------------------
# REST: POST /detect/frame
# ------------------------------------------------------------------

@router.post("/frame", response_model=DetectResponse)
async def detect_frame(config: DetectConfig, image: bytes):
    """Run detection on a single image frame.

    Accepts raw JPEG/PNG bytes as the request body and a
    `DetectConfig` JSON object.
    """
    core = _get_or_create_core(config)
    nparr = np.frombuffer(image, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Failed to decode image")
    result = core.process_frame(frame)
    return _build_response(result)


# ------------------------------------------------------------------
# WebSocket: ws /detect/stream
# ------------------------------------------------------------------

@router.websocket("/stream")
async def detect_stream(ws: WebSocket):
    """Streaming detection over WebSocket.

    1. Client sends SessionInit JSON (text frame).
    2. Server loads model, responds ``{"status": "ready"}``.
    3. Loop: client sends binary frame → server responds
       with DetectResponse JSON.
    """
    await ws.accept()

    try:
        init_raw = await ws.receive_text()
    except WebSocketDisconnect:
        return

    try:
        session = SessionInit.model_validate_json(init_raw)
    except (ValueError, KeyError, TypeError):
        await ws.send_text(json.dumps({"error": "Invalid SessionInit"}))
        await ws.close(code=1008)
        return

    config = _detect_config_to_pipeline_dict(
        DetectConfig(
            model_weights=session.model_weights,
            class_mode=session.class_mode,
            allowed_classes=session.allowed_classes,
            conf_threshold=session.conf_threshold,
            iou_threshold=session.iou_threshold,
            imgsz=session.imgsz,
            half=session.half,
            lanes=session.lanes,
            tracker_config=session.tracker_config,
            detect_every_n_frames=session.detect_every_n_frames,
            min_track_age_frames=session.min_track_age_frames,
            min_cross_distance_px=session.min_cross_distance_px,
        )
    )

    key = session.model_weights
    if key not in _core_cache:
        _core_cache[key] = DetectionCore(config)
        _core_cache[key].start()
    core = _core_cache[key]

    await ws.send_text(json.dumps({"status": "ready", "camera_id": session.camera_id}))

    frame_idx = 0
    try:
        while True:
            try:
                raw = await ws.receive_bytes()
            except WebSocketDisconnect:
                break

            nparr = np.frombuffer(raw, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if frame is None:
                continue

            result = core.process_frame(frame, frame_idx=frame_idx)
            frame_idx += 1
            resp = _build_response(result)
            await ws.send_text(resp.model_dump_json())
    finally:
        pass


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_response(result: dict) -> DetectResponse:
    return DetectResponse(
        frame_idx=result["frame_idx"],
        frame_timestamp=(
            result["frame_timestamp"].isoformat()
            if result.get("frame_timestamp") else ""
        ),
        tracks=[{
            "track_id": t["track_id"],
            "class_name": t["class_name"],
            "confidence": t["confidence"],
            "bbox": t["bbox"],
        } for t in result["tracks"]],
        raw_detections=result.get("raw_detections", []),
        events=result.get("events", []),
        occupancy=result.get("occupancy", {}),
        crossings=result.get("crossings", []),
        frame_tracks=result.get("frame_tracks", []),
        timing_ms=result["timing_ms"],
    )
