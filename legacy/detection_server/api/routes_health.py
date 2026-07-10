"""Health check endpoints."""
from fastapi import APIRouter

from detection_server.services.engine import engine

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/health/detailed")
def health_detailed():
    cameras = engine.list_cameras()
    result = {
        "status": "ok",
        "active_cameras": len(cameras),
        "cameras": {},
    }
    for cid in cameras:
        s = engine.get_status(cid)
        if s:
            result["cameras"][cid] = s
    return result
