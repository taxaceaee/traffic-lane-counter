"""Prometheus metrics for TrafficFlow.

Export via ``/api/admin/metrics`` Prometheus endpoint.
Provides per-camera latency, FPS, queue depth, event throughput,
and GPU utilization histograms.
"""
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from prometheus_client import REGISTRY

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

events_dropped_total = Counter(
    "trafficflow_events_dropped_total",
    "Events dropped due to queue full or DB errors",
    ["camera_id", "reason"],
)

# ── Queue ─────────────────────────────────────────────────────────────────────

queue_depth = Gauge(
    "trafficflow_storage_queue_depth",
    "Current StorageWorker queue depth per camera",
    ["camera_id"],
)

queue_dropped_total = Counter(
    "trafficflow_storage_queue_dropped_total",
    "Total events dropped due to queue full",
    ["camera_id"],
)

# ── Database ──────────────────────────────────────────────────────────────────

db_operation_seconds = Histogram(
    "trafficflow_db_operation_seconds",
    "DB operation latency (s)",
    ["operation"],
    buckets=(.001, .005, .01, .025, .05, .1, .25, .5, 1.0),
)

db_errors_total = Counter(
    "trafficflow_db_errors_total",
    "DB operation failures",
    ["operation"],
)

# ── GPU / System ──────────────────────────────────────────────────────────────

gpu_utilization = Gauge(
    "trafficflow_gpu_utilization_percent",
    "GPU utilization % (nvidia-smi)",
    ["gpu_id"],
)

gpu_memory_bytes = Gauge(
    "trafficflow_gpu_memory_bytes",
    "GPU memory usage (bytes)",
    ["gpu_id", "type"],
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


def count_dropped(camera_id: str, reason: str = "queue_full") -> None:
    events_dropped_total.labels(camera_id=camera_id, reason=reason).inc()
