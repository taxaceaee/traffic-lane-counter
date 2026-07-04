"""Admin API — protected health and metrics endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("trafficflow.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])
security = HTTPBearer(auto_error=False)


def _require_admin(cred: HTTPAuthorizationCredentials | None):
    if cred is None:
        raise HTTPException(401, "Missing authorization header")
    token = cred.credentials
    if not token:
        raise HTTPException(401, "Invalid token")
    from jose import JWTError, jwt

    from tf_api.api.routes_auth import ALGORITHM, SECRET_KEY
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("role") not in ("admin", "Administrator"):
            raise HTTPException(403, "Admin role required")
    except JWTError:
        raise HTTPException(401, "Invalid token")


@router.get("/health")
async def admin_health(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    _require_admin(cred)
    import torch
    return {
        "status": "ok",
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "gpu_available": torch.cuda.is_available(),
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none",
        "database_status": "connected",
    }


@router.get("/metrics")
async def admin_metrics(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    _require_admin(cred)
    from tf_common.monitoring.live_metrics import get_all_metrics
    return get_all_metrics()


@router.get("/system-health")
async def system_health(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    """Real-time comprehensive system health — CPU, RAM, disk, GPU, services, cameras, jobs."""
    _require_admin(cred)
    from tf_api.monitoring.system_metrics import get_latest
    return get_latest()


@router.get("/system-health/history")
async def system_health_history(
    limit: int = Query(60, ge=1, le=120, description="Number of historical data points"),
    cred: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Time-series history of system metrics for trend charts."""
    _require_admin(cred)
    from tf_api.monitoring.system_metrics import get_history
    return get_history(limit=limit)
