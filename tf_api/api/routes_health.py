"""Public health endpoint — readiness/liveness probe with dependency checks.

Sprint 4: includes DB, Redis, GPU status for production orchestration.
"""
from fastapi import APIRouter

from tf_api.services.health_checker import check_all

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health():
    return check_all()


@router.get("/health/worker")
def worker_health():
    """StorageWorker queue depth, backpressure, and dead-letter count."""
    try:
        from tf_worker.storage.storage_worker import storage_worker
        qsize = storage_worker.queue.qsize()
        max_q = storage_worker.max_queue_size
        dl_size = storage_worker.dead_letter_queue.qsize()
        return {
            "alive": storage_worker.is_alive(),
            "queue_depth": qsize,
            "max_queue": max_q,
            "dead_letter_count": dl_size,
            "backpressure": qsize > max_q // 2,
            "dropped_events": getattr(storage_worker, "dropped_count", 0),
        }
    except Exception as e:
        return {
            "alive": False,
            "error": str(e),
        }
