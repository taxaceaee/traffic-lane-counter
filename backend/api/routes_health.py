"""Public health endpoint — readiness/liveness probe with dependency checks.

Sprint 4: includes DB, Redis, GPU status for production orchestration.
"""
from fastapi import APIRouter

from backend.services.health_checker import check_all

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health():
    return check_all()


@router.get("/health/worker")
def worker_health():
    """StorageWorker queue depth, backpressure, and dead-letter count."""
    # There is no global storage_worker singleton; workers are created per-pipeline.
    return {
        "alive": False,
        "note": "Per-pipeline workers are not exposed via global singleton. Check pipeline logs.",
    }
