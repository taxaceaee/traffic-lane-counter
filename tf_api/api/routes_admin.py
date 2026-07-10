"""Admin API — protected health and metrics endpoints."""

import logging

from fastapi import APIRouter, Depends, Query

from tf_api.api.routes_auth import require_admin

logger = logging.getLogger("trafficflow.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])
@router.get("/health")
async def admin_health(_user: dict = Depends(require_admin)):
    import torch
    return {
        "status": "ok",
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "gpu_available": torch.cuda.is_available(),
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none",
        "database_status": "connected",
    }


@router.get("/metrics-json")
async def admin_metrics(_user: dict = Depends(require_admin)):
    from tf_common.monitoring.live_metrics import get_all_metrics
    return get_all_metrics()


@router.get("/system-health")
async def system_health(_user: dict = Depends(require_admin)):
    """Real-time comprehensive system health — CPU, RAM, disk, GPU, services, cameras, jobs."""
    from tf_api.monitoring.system_metrics import get_latest
    return get_latest()


@router.get("/system-health/history")
async def system_health_history(
    limit: int = Query(60, ge=1, le=120, description="Number of historical data points"),
    _user: dict = Depends(require_admin),
):
    """Time-series history of system metrics for trend charts."""
    from tf_api.monitoring.system_metrics import get_history
    return get_history(limit=limit)
