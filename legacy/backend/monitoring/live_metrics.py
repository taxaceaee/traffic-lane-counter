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

from backend.monitoring import metrics

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
        "timestamps": deque(maxlen=_WINDOW_SIZE),    # monotonic timestamps
        "latencies_ms": deque(maxlen=_WINDOW_SIZE),   # total latency per frame
        "fps": 0.0,
        "avg_latency_ms": 0.0,
        "gpu_util_pct": _probe_gpu(),
        "gpu_available": _gpu_available(),
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
            pass
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

def record_frame(camera_id: str, timing_ms: dict[str, float]) -> None:
    """Record one frame's timing data.

    Called from the live capture thread after every processed frame.
    """
    total_ms = sum(timing_ms.values())
    now = time.monotonic()

    with _lock:
        if camera_id not in _store:
            _store[camera_id] = _empty_store()
        entry = _store[camera_id]
        entry["timestamps"].append(now)
        entry["latencies_ms"].append(total_ms)

        # Compute rolling FPS over window
        cutoff = now - _FPS_WINDOW_SEC
        while entry["timestamps"] and entry["timestamps"][0] < cutoff:
            entry["timestamps"].popleft()
            # Keep latencies aligned — pop same number
        excess = _WINDOW_SIZE - len(entry["timestamps"])
        if excess > 0 and entry["latencies_ms"]:
            # Trim latencies to match timestamps window
            while len(entry["latencies_ms"]) > len(entry["timestamps"]):
                entry["latencies_ms"].popleft()
        count = len(entry["timestamps"])
        entry["fps"] = round(count / _FPS_WINDOW_SEC, 1) if count > 0 else 0.0

        # Average latency (last N frames)
        if entry["latencies_ms"]:
            entry["avg_latency_ms"] = round(
                sum(entry["latencies_ms"]) / len(entry["latencies_ms"]), 1
            )

    # Update Prometheus gauges (best-effort)
    try:
        metrics.camera_fps.labels(camera_id=camera_id).set(entry["fps"])
        metrics.camera_connected.labels(camera_id=camera_id).set(1)
    except Exception:
        pass


def record_stream_stopped(camera_id: str) -> None:
    """Mark a camera stream as disconnected."""
    with _lock:
        if camera_id in _store:
            _store[camera_id]["fps"] = 0.0
            _store[camera_id]["avg_latency_ms"] = 0.0
    try:
        metrics.camera_connected.labels(camera_id=camera_id).set(0)
        metrics.camera_fps.labels(camera_id=camera_id).set(0)
    except Exception:
        pass


def get_camera_metrics(camera_id: str) -> dict[str, Any]:
    """Return latest metrics snapshot for a camera."""
    with _lock:
        if camera_id not in _store:
            return {
                "camera_id": camera_id,
                "fps": 0.0,
                "avg_latency_ms": 0.0,
                "gpu_util_pct": _probe_gpu(),
                "gpu_available": _gpu_available(),
                "gpu_name": _GNU_NAME,
                "stream_active": False,
            }
        entry = _store[camera_id]
        return {
            "camera_id": camera_id,
            "fps": entry["fps"],
            "avg_latency_ms": entry["avg_latency_ms"],
            "gpu_util_pct": entry["gpu_util_pct"],
            "gpu_available": _gpu_available(),
            "gpu_name": _GNU_NAME,
            "stream_active": True,
        }


def get_all_metrics() -> dict[str, Any]:
    """Aggregate across all active cameras and system resources."""
    with _lock:
        fps_values = [s["fps"] for s in _store.values()]
        lat_values = [s["avg_latency_ms"] for s in _store.values() if s["avg_latency_ms"] > 0]
        active_cameras = [cid for cid, s in _store.items() if s["fps"] > 0]

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
