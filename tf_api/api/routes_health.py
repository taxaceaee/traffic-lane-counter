"""Public liveness and dependency-aware readiness endpoints."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from tf_api.services.health_checker import check_all, public_health

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health():
    return public_health()


@router.get("/readyz")
def readyz():
    result = check_all()
    status_code = 200 if result["status"] == "ready" else 503
    return JSONResponse(result, status_code=status_code)


@router.get("/health/worker")
def worker_health():
    """Report whether the standalone worker has sent a recent heartbeat."""
    import os

    redis_host = os.getenv("REDIS_HOST")
    if not redis_host:
        return {"alive": False, "reason": "REDIS_HOST is not configured"}
    try:
        import redis

        client = redis.Redis(
            host=redis_host,
            port=int(os.getenv("REDIS_PORT", "6379")),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        camera_count = client.get("trafficflow:worker:heartbeat")
        return {
            "alive": camera_count is not None,
            "camera_count": int(camera_count) if camera_count is not None else 0,
        }
    except Exception:
        return {"alive": False, "reason": "worker heartbeat unavailable"}
