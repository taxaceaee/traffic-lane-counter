"""Per-camera live metrics tracker — thread-safe, no external dependencies.

Computes real-time FPS, latency, and system resource utilisation from
actual frame processing data emitted by DetectionCore.

Compatible with every device:
- GPU stats use nvidia-ml-py7/pynvml when a CUDA GPU is detected
- Falls back gracefully to CPU-only reporting on CPU-only machines
- FPS and latency are always computed from real frame timings
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any

from tf_common.monitoring import metrics

logger = logging.getLogger("trafficflow.live_metrics")

# ── Tunables ──────────────────────────────────────────────────────────────────

_WINDOW_SIZE = 120        # keep last 120 frames of timing data
_FPS_WINDOW_SEC = 5.0     # compute FPS over last 5 seconds
_SYSTEM_POLL_SEC = 5.0    # how often to poll GPU / system stats

# ── Per-camera metrics store ──────────────────────────────────────────────────

_store: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _empty_store() -> dict[str, Any]:
    return {
        "process_timestamps": deque(maxlen=_WINDOW_SIZE),
        "output_timestamps": deque(maxlen=_WINDOW_SIZE),
        "latencies_ms": deque(maxlen=_WINDOW_SIZE),
        "process_fps": 0.0,
        "output_fps": 0.0,
        "source_fps": 0.0,
        "avg_latency_ms": 0.0,
        "gpu_util_pct": _probe_gpu(),
        "gpu_available": _gpu_available(),
        "stream_active": False,
        "status": "starting",
        "error": None,
        "last_frame_at": None,
    }


# ── GPU probing helpers (lazy, cached, device-agnostic) ──────────────────────

_GNU_CACHED: bool | None = None
_GNU_NAME: str = ""
_GNU_PROBE_LOCK = threading.Lock()


def _gpu_available() -> bool:
    global _GNU_CACHED, _GNU_NAME
    if _GNU_CACHED is not None:
        return _GNU_CACHED
    with _GNU_PROBE_LOCK:
        if _GNU_CACHED is not None:
            return _GNU_CACHED
        try:
            import torch
            _GNU_CACHED = torch.cuda.is_available()
            if _GNU_CACHED:
                _GNU_NAME = torch.cuda.get_device_name(0)
            return _GNU_CACHED
        except ImportError:
            _GNU_CACHED = False
            return False


def _probe_gpu() -> float:
    """Return GPU utilisation % or -1 if unavailable."""
    if not _gpu_available():
        return -1.0
    try:
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        return float(util.gpu)
    except Exception:
        try:
            import torch
            mem_allocated = torch.cuda.memory_allocated(0)
            mem_total = torch.cuda.get_device_properties(0).total_memory
            if mem_total > 0:
                return round(mem_allocated / mem_total * 100, 1)
        except Exception:
            logger.debug("Torch GPU memory fallback failed", exc_info=True)
        return -1.0


def _probe_memory_rss() -> float:
    """Return process RSS memory in bytes."""
    try:
        import psutil
        return float(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        return 0.0


def _probe_cpu_pct() -> float:
    """Return process CPU usage %."""
    try:
        import psutil
        return psutil.Process(os.getpid()).cpu_percent(interval=0)
    except Exception:
        return 50.0


def _probe_system_mem_pct() -> float:
    """Return system memory usage %."""
    try:
        import psutil
        return float(psutil.virtual_memory().percent)
    except Exception:
        return 50.0


# ── Public API ────────────────────────────────────────────────────────────────

def _trim_window(values: deque[float], now: float) -> None:
    cutoff = now - _FPS_WINDOW_SEC
    while values and values[0] < cutoff:
        values.popleft()


def _window_fps(values: deque[float]) -> float:
    count = len(values)
    return round(count / _FPS_WINDOW_SEC, 1) if count > 0 else 0.0


def record_frame(
    camera_id: str,
    timing_ms: dict[str, float],
    source_fps: float | None = None,
) -> None:
    """Record one frame's timing data.

    Called from the live capture thread after every processed frame.
    """
    total_ms = sum(timing_ms.values())
    now = time.monotonic()

    with _lock:
        if camera_id not in _store:
            _store[camera_id] = _empty_store()
        entry = _store[camera_id]
        entry["stream_active"] = True
        entry["status"] = "active"
        entry["error"] = None
        entry["last_frame_at"] = time.time()
        entry["process_timestamps"].append(now)
        entry["latencies_ms"].append(total_ms)
        if source_fps is not None:
            entry["source_fps"] = round(max(source_fps, 0.0), 1)
        _trim_window(entry["process_timestamps"], now)
        _trim_window(entry["output_timestamps"], now)
        entry["process_fps"] = _window_fps(entry["process_timestamps"])
        entry["output_fps"] = _window_fps(entry["output_timestamps"])

        # Average latency (last N frames)
        if entry["latencies_ms"]:
            entry["avg_latency_ms"] = round(
                sum(entry["latencies_ms"]) / len(entry["latencies_ms"]), 1
            )

    # Update Prometheus gauges (best-effort)
    try:
        metrics.camera_fps.labels(camera_id=camera_id).set(entry["process_fps"])
        metrics.camera_connected.labels(camera_id=camera_id).set(1)
    except Exception:
        logger.debug("Prometheus frame metric update failed", exc_info=True)


def record_output_frame(camera_id: str) -> None:
    """Record one frame actually emitted to the frontend MJPEG stream."""
    now = time.monotonic()
    with _lock:
        if camera_id not in _store:
            _store[camera_id] = _empty_store()
        entry = _store[camera_id]
        entry["stream_active"] = True
        if entry.get("status") in {"starting", "connecting"}:
            entry["status"] = "active"
        entry["output_timestamps"].append(now)
        _trim_window(entry["output_timestamps"], now)
        entry["output_fps"] = _window_fps(entry["output_timestamps"])


def record_stream_state(camera_id: str, status: str, error: str | None = None) -> None:
    """Record a lifecycle state for a live camera pipeline."""
    allowed = {"starting", "connecting", "active", "reconnecting", "error", "stopped"}
    if status not in allowed:
        raise ValueError(f"Unsupported stream status: {status}")
    with _lock:
        if camera_id not in _store:
            _store[camera_id] = _empty_store()
        entry = _store[camera_id]
        entry["status"] = status
        entry["error"] = error
        entry["stream_active"] = status in {"starting", "connecting", "active", "reconnecting"}


def record_stream_stopped(camera_id: str) -> None:
    """Mark a camera stream as disconnected."""
    record_stream_state(camera_id, "stopped")
    with _lock:
        if camera_id in _store:
            _store[camera_id]["process_fps"] = 0.0
            _store[camera_id]["output_fps"] = 0.0
            _store[camera_id]["avg_latency_ms"] = 0.0
            _store[camera_id]["stream_active"] = False
    try:
        metrics.camera_connected.labels(camera_id=camera_id).set(0)
        metrics.camera_fps.labels(camera_id=camera_id).set(0)
    except Exception:
        logger.debug("Prometheus stream metric update failed", exc_info=True)


def get_camera_metrics(camera_id: str) -> dict[str, Any]:
    """Return latest metrics snapshot for a camera."""
    with _lock:
        if camera_id not in _store:
            return {
                "camera_id": camera_id,
                "fps": 0.0,
                "process_fps": 0.0,
                "source_fps": 0.0,
                "output_fps": 0.0,
                "avg_latency_ms": 0.0,
                "gpu_util_pct": _probe_gpu(),
                "gpu_available": _gpu_available(),
                "gpu_name": _GNU_NAME,
                "stream_active": False,
                "status": "stopped",
                "error": None,
                "last_frame_at": None,
            }
        entry = _store[camera_id]
        return {
            "camera_id": camera_id,
            "fps": entry["process_fps"],
            "process_fps": entry["process_fps"],
            "source_fps": entry["source_fps"],
            "output_fps": entry["output_fps"],
            "avg_latency_ms": entry["avg_latency_ms"],
            "gpu_util_pct": entry["gpu_util_pct"],
            "gpu_available": _gpu_available(),
            "gpu_name": _GNU_NAME,
            "stream_active": entry["stream_active"],
            "status": entry.get("status", "starting"),
            "error": entry.get("error"),
            "last_frame_at": entry.get("last_frame_at"),
        }


def get_all_metrics() -> dict[str, Any]:
    """Aggregate across all active cameras and system resources."""
    with _lock:
        fps_values = [s["process_fps"] for s in _store.values()]
        lat_values = [s["avg_latency_ms"] for s in _store.values() if s["avg_latency_ms"] > 0]
        active_cameras = [cid for cid, s in _store.items() if s["process_fps"] > 0]

    return {
        "active_cameras": len(active_cameras),
        "avg_fps": round(sum(fps_values) / len(fps_values), 1) if fps_values else 0.0,
        "avg_latency_ms": round(sum(lat_values) / len(lat_values), 1) if lat_values else 0.0,
        "gpu_util_pct": _probe_gpu(),
        "gpu_available": _gpu_available(),
        "gpu_name": _GNU_NAME,
        "total_jobs": 0,           # populated by admin endpoint caller
        "active_jobs": 0,
        "total_events": 0,
    }


# ── Background system poller ─────────────────────────────────────────────────

_system_timer: threading.Timer | None = None
_system_lock = threading.Lock()


def _poll_system():
    """Periodically update GPU and memory metrics (Prometheus)."""
    global _system_timer
    try:
        gpu_pct = _probe_gpu()
        if gpu_pct >= 0 and _gpu_available():
            metrics.gpu_utilization.labels(gpu_id="0").set(gpu_pct)
        metrics.memory_rss_bytes.set(_probe_memory_rss())

        # Update GPU util in each camera store
        with _lock:
            for entry in _store.values():
                entry["gpu_util_pct"] = gpu_pct

    except Exception:
        logger.debug("system poll error", exc_info=True)
    finally:
        with _system_lock:
            _system_timer = threading.Timer(_SYSTEM_POLL_SEC, _poll_system)
            _system_timer.daemon = True
            _system_timer.start()


def _start_poller():
    with _system_lock:
        global _system_timer
        if _system_timer is not None and _system_timer.is_alive():
            return
        _system_timer = threading.Timer(_SYSTEM_POLL_SEC, _poll_system)
        _system_timer.daemon = True
        _system_timer.start()


def start_background_polling():
    """Start the background system metrics poller (called once at app startup)."""
    _start_poller()
