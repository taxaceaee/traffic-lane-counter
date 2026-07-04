"""Detection API — REST single-frame + WebSocket streaming.

Lanes are sent by the caller (backend) on each request — no hardcoded
config files are used.
"""

import json
import logging

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from detection_server.schemas.detect import DetectRequest, DetectResponse, StatusResponse
from detection_server.services.engine import DetectionEngine

logger = logging.getLogger("detection_server.detect")

router = APIRouter(prefix="/detect", tags=["detection"])

# Global engine — stores one DetectionCore per camera_id
engine = DetectionEngine()


# ── Config builder ──────────────────────────────────────────────────────────

def _detect_config_from_payload(payload: dict) -> DetectRequest:
    """Parse JSON payload into DetectConfig, validating embedded lane configs."""
    lanes_data = payload.get("lanes", [])
    from detection_server.schemas.detect import LaneDef, CountingLineDef, ROIRequest

    lanes = []
    for l in lanes_data:
        cl_data = l.get("counting_line")
        cl = CountingLineDef(**cl_data) if cl_data else None
        lanes.append(LaneDef(
            lane_id=l["lane_id"],
            name=l.get("name", ""),
            polygon=l["polygon"],
            counting_line=cl,
        ))

    roi_data = payload.get("roi")
    roi = ROIRequest(**roi_data) if roi_data else None

    return DetectRequest(
        camera_id=payload["camera_id"],
        model_weights=payload.get("model_weights", "yolo11s"),
        imgsz=payload.get("imgsz", 640),
        conf_threshold=payload.get("conf_threshold", 0.35),
        iou_threshold=payload.get("iou_threshold", 0.5),
        half=payload.get("half", False),
        allowed_classes=payload.get("allowed_classes", []),
        detect_every_n_frames=payload.get("detect_every_n_frames", 1),
        min_track_age_frames=payload.get("min_track_age_frames", 3),
        min_cross_distance_px=payload.get("min_cross_distance_px", 2.0),
        lanes=lanes,
        roi=roi,
    )


# ── REST: POST /detect/frame ──────────────────────────────────────────────

@router.post("/frame", response_model=DetectResponse)
async def detect_frame(
    config: str = Form(..., description="JSON DetectRequest with camera_id, lanes, model params etc."),
    image: UploadFile = File(..., description="JPEG/PNG image file"),
):
    """Run detection on a single image frame.

    Multipart form-data:
      - ``config``: JSON string with DetectRequest fields (camera_id, model_weights, lanes, roi, ...)
      - ``image``: JPEG or PNG image file

    Lanes in the config override any previous config for this camera_id.
    """
    try:
        payload = json.loads(config)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON in 'config' field")

    request = _detect_config_from_payload(payload)

    image_bytes = await image.read()
    nparr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "Failed to decode image")

    try:
        result = engine.process(request, frame)
    except Exception as e:
        logger.exception("Detection failed for camera %s", request.camera_id)
        raise HTTPException(500, f"Detection error: {e}")

    return result


# ── WebSocket: ws /detect/stream ───────────────────────────────────────────

@router.websocket("/stream")
async def detect_stream(ws: WebSocket):
    """Streaming detection over WebSocket.

    Protocol:
    1. Client sends JSON config (DetectRequest fields + optional lanes).
    2. Server responds {"status": "ready", "camera_id": "..."}.
    3. Client sends binary frame bytes.
    4. Server responds with JSON DetectResponse for each frame.

    Lane config can be included in step 1 only (persistent for the session),
    or re-sent as a JSON text frame at any time to update lanes mid-stream.
    """
    await ws.accept()

    config: DetectRequest | None = None

    try:
        while True:
            raw = await ws.receive()

            if "text" in raw:
                # Config update
                try:
                    payload = json.loads(raw["text"])
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps({"error": "Invalid JSON"}))
                    continue

                cmd = payload.get("cmd", "configure")
                if cmd == "configure":
                    config = _detect_config_from_payload(payload)
                    await ws.send_text(json.dumps({
                        "status": "ready",
                        "camera_id": config.camera_id,
                        "lanes": len(config.lanes),
                    }))
                    logger.info("WS config for camera %s: %d lanes", config.camera_id, len(config.lanes))
                elif cmd == "reset":
                    if config:
                        engine.reset(config.camera_id)
                        await ws.send_text(json.dumps({"status": "reset", "camera_id": config.camera_id}))
                else:
                    await ws.send_text(json.dumps({"error": f"Unknown cmd: {cmd}"}))

            elif "bytes" in raw:
                if config is None:
                    await ws.send_text(json.dumps({
                        "error": "No config. Send configure message first: {\"cmd\":\"configure\",\"camera_id\":\"...\",...}"
                    }))
                    continue

                image_bytes = raw["bytes"]
                nparr = np.frombuffer(image_bytes, np.uint8)
                frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if frame is None:
                    continue

                try:
                    result = engine.process(config, frame)
                    await ws.send_text(result.model_dump_json())
                except Exception as e:
                    logger.exception("WS detection error for %s", config.camera_id)
                    await ws.send_text(json.dumps({"error": str(e)}))

    except WebSocketDisconnect:
        logger.info("WS client disconnected for camera %s", config.camera_id if config else "unknown")
    except Exception as e:
        logger.exception("WS unexpected error")
        try:
            await ws.close(code=1011)
        except Exception:
            pass


# ── Status ─────────────────────────────────────────────────────────────────

@router.get("/status/{camera_id}", response_model=StatusResponse)
async def get_detection_status(camera_id: str):
    """Get detection status for a specific camera."""
    status = engine.get_status(camera_id)
    if status is None:
        raise HTTPException(404, f"No active detection session for camera {camera_id}")
    return status


@router.get("/cameras")
async def list_detection_cameras():
    """List all camera_ids with active detection sessions."""
    return {"cameras": engine.list_cameras()}
