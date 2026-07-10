"""System-wide metrics collector — real-time CPU, memory, disk, GPU, uptime, and service status.

Compatible with every device:
- Uses psutil for cross-platform system metrics (CPU, RAM, disk)
- GPU stats via pynvml when available, graceful fallback to CPU-only
- Thread-safe rolling window for trend data
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger("trafficflow.system_metrics")

_HISTORY_MAX = 120
_POLL_INTERVAL = 5.0

_store: dict[str, Any] = {
    "latest": {},
    "history": deque(maxlen=_HISTORY_MAX),
    "start_time": time.time(),
}
_lock = threading.Lock()
_timer: threading.Timer | None = None


def _get_cpu_pct() -> float:
    try:
        import psutil
        return psutil.cpu_percent(interval=0)
    except Exception:
        return 0.0


def _get_memory_pct() -> float:
    try:
        import psutil
        return float(psutil.virtual_memory().percent)
    except Exception:
        return 0.0


def _get_disk_pct() -> float:
    try:
        import psutil
        return float(psutil.disk_usage("/").percent)
    except Exception:
        return 0.0


def _get_gpu_info() -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "util_pct": -1.0, "name": ""}
    try:
        import torch
        if torch.cuda.is_available():
            result["available"] = True
            result["name"] = torch.cuda.get_device_name(0)
            try:
                import pynvml
                pynvml.nvmlInit()
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                result["util_pct"] = float(util.gpu)
            except Exception:
                try:
                    mem_allocated = torch.cuda.memory_allocated(0)
                    mem_total = torch.cuda.get_device_properties(0).total_memory
                    if mem_total > 0:
                        result["util_pct"] = round(mem_allocated / mem_total * 100, 1)
                except Exception:
                    pass
    except ImportError:
        pass
    return result


def _get_uptime() -> str:
    elapsed = time.time() - _store["start_time"]
    days, rem = divmod(int(elapsed), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def _get_uptime_seconds() -> float:
    return time.time() - _store["start_time"]


def _collect_snapshot() -> dict[str, Any]:
    cpu = _get_cpu_pct()
    mem = _get_memory_pct()
    disk = _get_disk_pct()
    gpu = _get_gpu_info()

    db_status = "unknown"
    redis_status = "unknown"
    db_latency_ms: float | None = None
    try:
        from backend.services.health_checker import check_database, check_redis
        db_result = check_database()
        db_status = db_result.get("status", "unknown")
        db_latency_ms = db_result.get("latency_ms")
        redis_result = check_redis()
        redis_status = redis_result.get("status", "unknown")
    except Exception:
        pass

    active_cameras = 0
    total_cameras = 0
    try:
        from backend.monitoring.live_metrics import get_all_metrics
        metrics_data = get_all_metrics()
        active_cameras = metrics_data.get("active_cameras", 0)
    except Exception:
        pass
    try:
        from backend.api.routes_cameras import get_camera_list
        all_cams = get_camera_list()
        total_cameras = len(all_cams)
    except Exception:
        pass

    ws_connections = 0
    try:
        from backend.api.routes_ws import get_connection_stats
        stats = get_connection_stats()
        ws_connections = stats.get("global_connections", 0)
    except Exception:
        pass

    active_jobs = 0
    total_jobs = 0
    try:
        from backend.api.routes_jobs import get_job_stats
        job_stats = get_job_stats()
        active_jobs = job_stats.get("active", 0)
        total_jobs = job_stats.get("total", 0)
    except Exception:
        pass

    import platform
    return {
        "timestamp": time.time(),
        "cpu_pct": round(cpu, 1),
        "memory_pct": round(mem, 1),
        "disk_pct": round(disk, 1),
        "gpu": dict(gpu),
        "uptime": _get_uptime(),
        "uptime_seconds": round(_get_uptime_seconds()),
        "database_status": db_status,
        "database_latency_ms": db_latency_ms,
        "redis_status": redis_status,
        "active_cameras": active_cameras,
        "total_cameras": total_cameras,
        "ws_connections": ws_connections,
        "active_jobs": active_jobs,
        "total_jobs": total_jobs,
        "platform": platform.system(),
        "python_version": platform.python_version(),
    }


def _poll():
    global _timer
    try:
        snapshot = _collect_snapshot()
        with _lock:
            _store["latest"] = snapshot
            _store["history"].append(snapshot)
    except Exception as exc:
        logger.debug("system metrics poll error: %s", exc)
    finally:
        _timer = threading.Timer(_POLL_INTERVAL, _poll)
        _timer.daemon = True
        _timer.start()


def get_latest() -> dict[str, Any]:
    with _lock:
        if _store["latest"]:
            return dict(_store["latest"])
    return _collect_snapshot()


def get_history(limit: int = 60) -> list[dict[str, Any]]:
    with _lock:
        data = list(_store["history"])
    if limit > 0 and len(data) > limit:
        data = data[-limit:]
    return data


def start_collector():
    global _timer
    with _lock:
        if _timer is not None and _timer.is_alive():
            return
    snapshot = _collect_snapshot()
    with _lock:
        _store["latest"] = snapshot
        _store["history"].append(snapshot)
    _timer = threading.Timer(_POLL_INTERVAL, _poll)
    _timer.daemon = True
    _timer.start()
