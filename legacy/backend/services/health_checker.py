"""Health checker — validates DB, Redis, GPU dependencies with TTL caching.

Used by /api/health endpoint for readiness/liveness probes.
Results are cached with a TTL to avoid hammering dependencies.
"""
import logging
import os
import time
from typing import Any

logger = logging.getLogger("trafficflow.health")

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = float(os.getenv("HEALTH_CACHE_TTL", "5.0"))  # default 5s
_REDIS_TIMEOUT = float(os.getenv("REDIS_CONNECT_TIMEOUT", "1.0"))  # 1s connect timeout


def _cached(key: str, ttl: float) -> Any | None:
    entry = _CACHE.get(key)
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None


def _set_cache(key: str, value: Any) -> None:
    _CACHE[key] = (time.monotonic(), value)
    # Prune old entries
    stale = [k for k, (ts, _) in _CACHE.items() if (time.monotonic() - ts) > _CACHE_TTL * 2]
    for k in stale:
        _CACHE.pop(k, None)


def check_database() -> dict[str, Any]:
    cached = _cached("db", _CACHE_TTL)
    if cached is not None:
        return dict(cached)
    result = {"status": "unknown", "latency_ms": None}
    try:
        from backend.db.session import SessionLocal
        from sqlalchemy import text
        t0 = time.monotonic()
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        result["status"] = "healthy"
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        # Resolve any previous db_connection_failed alert
        _resolve_alert("db_connection_failed")
    except Exception as exc:
        result["status"] = "unhealthy"
        result["error"] = str(exc)
        _emit_alert("critical", "Database Disconnected", f"DB check failed: {exc}", alert_type="db_connection_failed")
    _set_cache("db", result)
    return result


def _emit_alert(severity: str, title: str, message: str, alert_type: str = "general", camera_id: str | None = None) -> None:
    try:
        from backend.services.alert_service import alert_service
        alert_service.emit(
            severity=severity, title=title, message=message,
            alert_type=alert_type, camera_id=camera_id,
        )
    except Exception:
        pass


def _resolve_alert(alert_type: str, camera_id: str | None = None) -> None:
    try:
        from backend.services.alert_service import alert_service
        alert_service.resolve(alert_type=alert_type, camera_id=camera_id)
    except Exception:
        pass


def check_redis() -> dict[str, Any]:
    cached = _cached("redis", _CACHE_TTL)
    if cached is not None:
        return dict(cached)
    result = {"status": "unknown", "latency_ms": None}
    try:
        import socket
        t0 = time.monotonic()
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", 6379))
        sock = socket.create_connection((host, port), timeout=_REDIS_TIMEOUT)
        sock.sendall(b"PING\r\n")
        sock.recv(1024)
        sock.close()
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["status"] = "healthy"
    except OSError as exc:
        logger.debug("Redis check: %s", exc)
        result["status"] = "unavailable"
    except Exception as exc:
        result["status"] = "unhealthy"
        result["error"] = str(exc)
    _set_cache("redis", result)
    return result


def check_gpu() -> dict[str, Any]:
    cached = _cached("gpu", _CACHE_TTL)
    if cached is not None:
        return dict(cached)
    result = {"available": False, "name": ""}
    deadline = time.monotonic() + _REDIS_TIMEOUT
    try:
        import torch
        result["available"] = torch.cuda.is_available()
        if result["available"] and time.monotonic() < deadline:
            result["name"] = torch.cuda.get_device_name(0)
            result["memory_utilization"] = _get_gpu_util()
    except ImportError:
        result["available"] = False
    _set_cache("gpu", result)
    return result


def _get_gpu_util() -> float | None:
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        return float(util.gpu)
    except Exception:
        return None


def check_all() -> dict[str, Any]:
    deps = {
        "database": check_database(),
        "redis": check_redis(),
        "gpu": check_gpu(),
    }
    # healthy = all required deps are healthy
    # degraded = at least one dep is unhealthy (not just unavailable)
    # ok = no deps are unhealthy (maybe some unavailable, which is fine)
    unhealthy = [k for k, v in deps.items() if v.get("status") == "unhealthy"]
    return {
        "status": "ok",
        "dependencies": deps,
        "unhealthy": unhealthy if unhealthy else None,
    }
