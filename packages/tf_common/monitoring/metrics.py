"""Prometheus metrics for TrafficFlow.

Export via ``/api/admin/metrics`` Prometheus endpoint.
Provides per-camera latency, FPS, queue depth, event throughput,
and GPU utilization histograms.
"""

try:
    from prometheus_client import REGISTRY, Counter, Gauge, Histogram, generate_latest
except ImportError:  # pragma: no cover - defensive fallback for thin environments
    class _NoopMetric:
        def labels(self, **_kwargs):
            return self

        def observe(self, *_args, **_kwargs):
            return None

        def inc(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    def Counter(*_args, **_kwargs):  # type: ignore[misc]
        return _NoopMetric()

    def Gauge(*_args, **_kwargs):  # type: ignore[misc]
        return _NoopMetric()

    def Histogram(*_args, **_kwargs):  # type: ignore[misc]
        return _NoopMetric()

    REGISTRY = None

    def generate_latest(_registry=None):  # type: ignore[override]
        return b"# prometheus_client not installed\n"

# ── Frame processing ──────────────────────────────────────────────────────────

frame_processing_seconds = Histogram(
    "trafficflow_frame_processing_seconds",
    "Per-frame processing time by stage (s)",
    ["camera_id", "stage"],
    buckets=(.005, .01, .025, .05, .075, .1, .25, .5, .75, 1.0, 2.5, 5.0),
)

# ── Events ───────────────────────────────────────────────────────────────────

events_total = Counter(
    "trafficflow_events_total",
    "Total crossing events processed",
    ["camera_id", "lane_id", "direction"],
)

# ── Queue ─────────────────────────────────────────────────────────────────────

queue_depth = Gauge(
    "trafficflow_storage_queue_depth",
    "Current StorageWorker queue depth per camera",
    ["camera_id"],
)

# ── GPU / System ──────────────────────────────────────────────────────────────

gpu_utilization = Gauge(
    "trafficflow_gpu_utilization_percent",
    "GPU utilization % (nvidia-smi)",
    ["gpu_id"],
)

memory_rss_bytes = Gauge(
    "trafficflow_memory_rss_bytes",
    "Process RSS memory (bytes)",
)

# ── Camera health ─────────────────────────────────────────────────────────────

camera_connected = Gauge(
    "trafficflow_camera_connected",
    "1 = connected, 0 = disconnected",
    ["camera_id"],
)

camera_fps = Gauge(
    "trafficflow_camera_fps",
    "Frames per second processed",
    ["camera_id"],
)


def metrics_endpoint() -> tuple[str, int]:
    """Return Prometheus text metrics + HTTP 200."""
    return generate_latest(REGISTRY).decode("utf-8"), 200


def observe_frame(camera_id: str, stage: str, elapsed_s: float) -> None:
    frame_processing_seconds.labels(camera_id=camera_id, stage=stage).observe(elapsed_s)


def count_event(camera_id: str, lane_id: str, direction: str) -> None:
    events_total.labels(camera_id=camera_id, lane_id=lane_id, direction=direction).inc()
