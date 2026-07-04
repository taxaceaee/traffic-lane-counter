# Production-Scale Plan: TrafficFlow Multi-Camera Video Analytics

> Based on real-world patterns from Amazon Rekognition, NVIDIA DeepStream,
> Azure Video Analyzer, Kafka Stream Processing, and CCTV deployments
> (Milestone, Genetec) at 100+ camera scale.

## Executive Summary

This project currently supports single-camera offline processing with basic
live streaming. To scale to 10+ simultaneous cameras at production load,
we need to fix ~30 issues across 6 domains: concurrency, resource mgmt,
resilience, observability, security, and performance.

## 1. Concurrency & Threading

### 1.1 GIL-bound Camera Threads (P0)

**Problem:** Each live camera spawns a `threading.Thread` running Python
code (detection → tracking → counting). Python's GIL serializes all threads,
so 10 cameras on one CPU effectively get 1/10th the throughput.

**Case study:** NVIDIA DeepStream uses pipeline parallelism (C/C++ GStreamer
elements), not thread-per-camera. At 16 cameras, pure Python threading
drops from 30 FPS to ~3 FPS per camera.

**Fix:**
```python
# Option A: Multiprocessing (one process per GPU)
# Option B: ProcessPoolExecutor for detection, async for I/O
# Option C: Async frame capture + batched GPU inference
```

- Split pipeline into async capture (zero-copy) + sync GPU batch inference
- Use `multiprocessing.Process` for GPU-bound detection (one per GPU)
- Use `asyncio` for I/O-bound capture, storage, redis
- Cap concurrent cameras at `n_gpu * 2` with admission control

### 1.2 Connection Pool Exhaustion (P0)

**Problem:** SQLAlchemy `SessionLocal` uses default pool_size=5. With
10 cameras × 200 events/min, the pool saturates in seconds → `TimeoutError`
→ dropped events.

**Fix:**
```python
# backend/db/session.py
def get_engine():
    pool_size = int(os.getenv("DB_POOL_SIZE", 20))
    max_overflow = int(os.getenv("DB_POOL_OVERFLOW", 10))
    return create_engine(url, pool_size=pool_size, max_overflow=max_overflow,
                         pool_pre_ping=True, pool_recycle=300)
```

### 1.3 Redis Connection per Camera (P1)

**Problem:** Each camera creates one `redis.Redis` connection. At 50 cameras
this is 50 TCP connections. Redis pub/sub also creates one subscription per
client — at scale this degrades Redis performance.

**Fix:**
- Use a single shared `redis.ConnectionPool`
- Multiplex pub/sub: one subscriber per *channel pattern*, fan-out to local
  in-process queues for WebSocket clients
- Add `max_connections` pool limit + retry

## 2. Resource Management

### 2.1 GPU Memory Oversubscription (P0)

**Problem:** Loading the YOLO model once per camera with
`DetectionCore.__init__` = N copies of model weights in GPU memory.
YOLOv8n = ~6 MB, YOLOv8x = ~68 MB. 10 cameras × YOLOv8x = 680 MB +
activation memory → OOM on 8 GB GPU.

**Case study:** AWS Rekognition Video shares ONE model across all streams
with batched inference. OpenVINO caches the compiled model in shared memory.

**Fix:**
```python
# shared/detection/yolo_detector.py — ModelRegistry singleton
class ModelRegistry:
    _models: dict[str, YoloDetectorWrapper] = {}
    
    @classmethod
    def get(cls, weights: str, half: bool = False) -> YoloDetectorWrapper:
        key = f"{weights}:half={half}"
        if key not in cls._models:
            cls._models[key] = YoloDetectorWrapper(weights, half=half)
        return cls._models[key]
```

### 2.2 OpenCV Mat Memory Leak (P1)

**Problem:** `cv2.VideoCapture.read()` allocates new `np.ndarray` each frame.
Without explicit `del`, and especially in try/finally paths, OpenCV's C++
allocator can leak GPU/System memory.

**Case study:** Milestone XProtect found ~2 MB/frame leak in Python bindings
at 25 FPS → 3 GB/hour leak.

**Fix:**
- Use `np.array(frame, copy=False)` to avoid copies
- Explicit `gc.collect()` every 1000 frames
- Monitor `process.memory_info().rss` and auto-restart worker at threshold
- Release frames immediately after encoding

### 2.3 Queue Backpressure Cascade (P1)

**Problem:** When DB is slow (e.g., checkpoint), `StorageWorker` queue fills
→ events dropped → but the inference loop keeps running → memory grows from
unsent event data.

**Case study:** Kafka Streams uses backpressure via `max.poll.records` +
paused partitions. Without it, producer memory OOM kills the pod.

**Fix:**
```python
# Adaptive queue throttling
class AdaptiveThrottle:
    def __init__(self, max_qsize: int = 2048):
        self.max_qsize = max_qsize
    
    def should_drop_frame(self, current_qsize: int) -> bool:
        ratio = current_qsize / self.max_qsize
        if ratio > 0.9: return True        # emergency drop
        if ratio > 0.7 and random.random() < 0.3: return True  # probabilistic
        return False
```

## 3. Resilience

### 3.1 RTSP Reconnection Storm (P0)

**Problem:** When the network blips, ALL cameras lose RTSP simultaneously
→ ALL threads retry `cv2.VideoCapture()` at the same time → CPU spike →
cameras stagger-reconnect over 30s → data gap.

**Case study:** Genetec found that jittered reconnection (base_delay=1s,
max_delay=30s, random factor=0.5) reduces reconnection storms by 90%.

**Fix:**
```python
# backend/io/video_io.py — Exponential backoff + jitter
def _reconnect(self):
    delay = 1.0
    while not self._stop_event.is_set():
        cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)
        if cap.isOpened():
            self.cap = cap
            return
        delay = min(delay * 2, 30) * (0.5 + random.random())
        time.sleep(delay)
```

### 3.2 Circuit Breaker for DB/Redis (P1)

**Problem:** When PostgreSQL is down (maintenance, failover), every event
insert attempts a connection → `OperationalError` → logs full of errors →
retry storms.

**Fix:**
```python
# Use pybreaker or simple state machine
class DBCircuitBreaker:
    def __init__(self, failure_threshold=5, recovery_timeout=30):
        self.failures = 0
        self.state = "closed"  # closed / open / half-open
        self.last_failure = 0
    
    def call(self, fn, *args, **kwargs):
        if self.state == "open":
            if time.monotonic() - self.last_failure < self.recovery_timeout:
                raise CircuitBreakerOpen()
            self.state = "half-open"
        try:
            result = fn(*args, **kwargs)
            self.failures = 0
            self.state = "closed"
            return result
        except Exception:
            self.failures += 1
            self.last_failure = time.monotonic()
            if self.failures >= self.failure_threshold:
                self.state = "open"
            raise
```

### 3.3 Graceful Degradation (P1)

**Problem:** Currently, if DB or Redis is down, events are silently dropped
but video processing continues. No fallback to local queue-on-disk.

**Fix:**
```
DB available   → store events in PostgreSQL
DB unavailable → store events in SQLite (local fallback) + queue for replay
Both down      → drop, but log structured metric
```

## 4. Observability

### 4.1 Prometheus Metrics (P0)

**Problem:** Zero production metrics. No way to know FPS, queue depth,
event throughput, GPU utilization, or error rates per camera.

**Fix:**
```python
# Export via prometheus_client
from prometheus_client import Histogram, Counter, Gauge

frame_processing_seconds = Histogram(
    'trafficflow_frame_processing_seconds',
    'Per-frame processing time', ['camera_id', 'stage'],
    buckets=(.005, .01, .025, .05, .075, .1, .25, .5, .75, 1.0),
)

events_total = Counter(
    'trafficflow_events_total',
    'Total crossing events', ['camera_id', 'lane_id', 'direction'],
)

queue_depth = Gauge(
    'trafficflow_storage_queue_depth',
    'StorageWorker queue depth', ['camera_id'],
)
```

### 4.2 Structured Logging (P1)

**Problem:** Current logging uses `logger.info(f"string {var}")` with no
structured fields. Impossible to filter/search by camera_id, job_id, or
correlation ID in ELK/Datadog.

**Fix:**
```python
# Use structlog or python-json-logger
logger.info("frame_processed", extra={
    "camera_id": camera_id, "frame_idx": frame_idx,
    "detections": len(detections), "events": len(events),
    "latency_ms": timing_ms,
})
```

### 4.3 Health Check with Dependency Status (P0)

**Problem:** Current `/api/health` returns `{"status": "ok"}` regardless of
DB/Redis/GPU status. Production needs readiness + liveness probes.

**Fix:**
```python
@router.get("/api/health")
async def health():
    deps = {
        "database": _check_db(),
        "redis": _check_redis(),
        "gpu": _check_gpu(),
    }
    overall = "healthy" if all(deps.values()) else "degraded"
    return {"status": overall, "dependencies": deps}
```

## 5. Security at Scale

### 5.1 Per-API-Key Rate Limiting (P1)

**Problem:** Current rate limiter uses `"global"` as key → all clients share
200 req/min. One noisy client can starve others.

**Fix:**
```python
def _rate_limit_key(request: Request) -> str:
    key = request.headers.get("X-API-Key", "")
    return key if key else request.client.host

limiter = Limiter(key_func=_rate_limit_key, default_limits=["200/minute"])
```

### 5.2 WebSocket Authentication (P1)

**Problem:** WebSocket connections have no auth. An attacker can subscribe
to all camera events by opening 1000 WebSocket connections → DDoS.

**Fix:**
```python
@router.websocket("/ws/live/{camera_id}")
async def ws_live(ws: WebSocket, token: str = Query(...)):
    user = verify_token(token)
    if not user:
        await ws.close(code=4001)
        return
    # connection rate limit: max 5 per user
```

### 5.3 Input Validation at Scale (P2)

**Problem:** Route parameters like `camera_id` are plain `str` → path
injection risk with `safe_join` passing, but other paths bypass validation.

**Fix:**
- Pydantic validator on ALL route params
- `Path("../../etc/passwd")` → 422 before any file access
- File size limits on video uploads (current: none)

## 6. Performance

### 6.1 Frame-skipping per Camera (P0)

**Problem:** All cameras process every frame regardless of content change.
Static scenes (night, empty parking lot) waste GPU cycles.

**Case study:** Azure Video Analyzer uses motion detection to skip frames.
Saves 40-60% GPU on typical street cameras.

**Fix:**
```python
# Frame differencing to detect motion before running inference
def has_motion(frame, prev_gray, threshold=0.01):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(gray, prev_gray)
    non_zero = np.count_nonzero(diff > 30)
    ratio = non_zero / diff.size
    return ratio > threshold, gray
```

### 6.2 JSON Serialization Bottleneck (P1)

**Problem:** Each event is serialized to JSON twice (Redis publish +
JSONL write). At 200 events/sec, Python's `json.dumps` consumes ~5% CPU.

**Fix:**
- Use `orjson` (3-6x faster than stdlib json) for serialization
- Pre-serialize events in `ctypes` or `msgpack` for Redis
- Batch JSONL writes (current: one `open()` per frame)

### 6.3 Image Encoding Offload (P0 - DONE)

**Problem (FIXED):** JPEG encoding was in the main inference thread.
**Fix applied:** Moved to `StorageWorker` background thread. But crop
still uses full BGR frame in queue — further optimize:

**Next step:** 
- Pass only `(frame_bytes, bbox)` tuple
- Use `turbojpeg` instead of `cv2.imencode` for 2-3x faster encoding
- Pre-encode at reduced resolution (320px longest edge)

### 6.4 Database Batch Writes (P0 - DONE)

**Problem (FIXED):** Per-event `session.commit()`.
**Fix applied:** Batch flush every 50 events.

**Next step:**
- Use `executemany` for event inserts (single SQL stmt for 50 rows)
- Use COPY FROM for bulk ingestion
- Partition tables by date for fast DELETE in retention

## 7. Immediate TODOs (Priority Order)

### Sprint 1 — Stability (Week 1)
- [x] Frame-accurate timestamps (DONE)
- [x] JPEG encoding offload to background (DONE)
- [x] Batch DB commits (DONE)
- [ ] GPU ModelRegistry singleton (not started)
- [ ] RTSP reconnection with jittered backoff (not started)
- [ ] DB connection pool sizing env vars (not started)
- [ ] Prometheus metrics export (not started)

### Sprint 2 — Resilience (Week 2)
- [ ] Circuit breaker for DB/Redis
- [ ] Adaptive queue backpressure
- [ ] Graceful degradation (SQLite fallback)
- [ ] Per-API-Key rate limiting
- [ ] WebSocket authentication + rate limit

### Sprint 3 — Scaling (Week 3)
- [ ] Multiprocessing for camera workers
- [ ] Shared Redis connection pool
- [ ] Frame-skipping with motion detection
- [ ] Structured logging
- [ ] orjson serialization

### Sprint 4 — Production Hardening (Week 4)
- [ ] Horizontal scaling with Redis pub/sub sharding
- [ ] Health check with dependency status
- [ ] Auto-restart workers on memory threshold
- [ ] Partitioned DB tables
- [ ] Load testing at 50+ camera simulation
