"""StorageWorker — event-driven, tiered storage for vehicle counting.

Architecture
------------
The main inference loop enqueues crossing events via ``StorageWorker.enqueue()``.
A background daemon thread drains the queue and performs:

1. Crop extraction via ``CropStorage`` protocol (local disk or S3).
2. DB write via ``EventRepository`` protocol (one row per crossing).
3. Aggregate roll-up via ``AggregateRepository`` protocol (atomic UPSERT).
4. Real-time publish via ``StreamPublisher`` protocol (Redis Pub/Sub).

Operational hardening for 24/7:
- Backpressure: queues dropped events to a dead-letter list instead of silent drop
- Circuit breaker: prevents cascade failure when DB/Redis is down
- Watchdog: monitors queue depth and logs warnings at thresholds
- Graceful shutdown: drains queue fully before exit
- Memory guard: limits in-flight payloads to prevent OOM
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

from tf_common.circuit_breaker import CircuitBreakerOpen, db_breaker
from tf_db.storage.crop_protocol import CropStorage
from tf_db.storage.pubsub_protocol import StreamPublisher

logger = logging.getLogger("trafficflow.storage")


def _floor_to_window(dt: datetime, minutes: int) -> datetime:
    return dt.replace(second=0, microsecond=0, minute=(dt.minute // minutes) * minutes)


def _floor_to_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


WINDOWS = {
    "1min":  lambda dt: _floor_to_window(dt, 1),
    "5min":  lambda dt: _floor_to_window(dt, 5),
    "1hour": lambda dt: dt.replace(minute=0, second=0, microsecond=0),
    "1day":  lambda dt: _floor_to_day(dt),
}

# Watchdog thresholds
_QUEUE_WARN_THRESHOLD = 512
_QUEUE_CRIT_THRESHOLD = 1024
_DEAD_LETTER_MAX = 10_000


def _emit_queue_alert(camera_id: str, qsize: int, maxsize: int) -> None:
    try:
        from tf_common.alert_service import alert_service
        alert_service.emit(
            severity="warning",
            title="Storage Backpressure",
            message=f"Processing queue {qsize}/{maxsize} — events being dropped",
            camera_id=camera_id,
            alert_type="queue_backpressure",
        )
    except Exception:
        pass


class StorageWorker:
    """Consumes crossing events from the inference loop and persists them.

    Parameters
    ----------
    storage_root:
        Base directory for all file artefacts (crops, clips, exports).
    adapter:
        ``RepositoryBundle`` protocol adapter for DB persistence.  ``None`` =
        file-only mode (no database).
    crop_storage:
        ``CropStorage`` protocol for vehicle-crop images.  ``None`` =
        auto-create a ``LocalCropStorage`` from ``config``.
    publisher:
        ``StreamPublisher`` protocol for real-time event streaming.
        ``None`` = no streaming.
    config:
        Dict from the YAML ``storage`` block.
    queue_maxsize:
        Internal queue depth.  Events dropped when full (non-blocking put).
    """

    def __init__(
        self,
        storage_root: str | Path,
        adapter: Any = None,
        crop_storage: CropStorage | None = None,
        publisher: StreamPublisher | None = None,
        config: dict[str, Any] | None = None,
        queue_maxsize: int = 2048,
        db_session_factory: Callable[[], Any] | None = None,
    ):
        cfg = config or {}
        self.storage_root = Path(storage_root)
        self.publisher = publisher

        if adapter is not None:
            self.adapter = adapter
        elif db_session_factory is not None:
            raise ImportError(
                "StorageWorker requires an explicit 'adapter' argument. "
                "Inject a RepositoryBundle adapter from the caller instead of "
                "passing db_session_factory."
            )
        else:
            self.adapter = None

        if crop_storage is not None:
            self.crop_storage = crop_storage
        elif not cfg.get("save_vehicle_crop", True):
            self.crop_storage = None
        else:
            from tf_worker.storage.local_crop_storage import LocalCropStorage
            self.crop_storage = LocalCropStorage(
                storage_root=self.storage_root,
                format=cfg.get("crop_format", "jpg"),
                quality=int(cfg.get("crop_quality", 80)),
                max_px=int(cfg.get("crop_max_px", 320)),
            )

        self.agg_windows: list[str] = cfg.get("aggregate_windows", ["1min", "5min", "1hour", "1day"])

        self._queue: Queue = Queue(maxsize=queue_maxsize)
        self._stop = threading.Event()
        self.dropped_events: int = 0
        self.total_processed: int = 0
        self.total_errors: int = 0
        self._dead_letter: list[dict] = []
        self._dead_letter_lock = threading.Lock()
        self._watchdog_logged = False
        self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="StorageWorker")
        self._thread.start()

    # ------------------------------------------------------------------
    # Adaptive backpressure — let the pipeline caller know when to drop
    # ------------------------------------------------------------------

    @property
    def backpressure_ratio(self) -> float:
        """0.0 = empty, 1.0 = full.  Used by caller for adaptive frame skip."""
        qsize = self._queue.qsize()
        maxsize = self._queue.maxsize
        return qsize / maxsize if maxsize > 0 else 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        camera_id: str,
        job_id: str,
        lane_id: str,
        track_id: int,
        vehicle_type: str,
        direction: str,
        confidence: float,
        frame_id: int,
        timestamp: datetime,
        frame: Any | None = None,
        bbox: list[float] | None = None,
        crop_bytes: bytes | None = None,
    ) -> None:
        """Non-blocking enqueue with backpressure detection."""
        payload = {
            "camera_id": camera_id,
            "job_id": job_id,
            "lane_id": lane_id,
            "track_id": track_id,
            "vehicle_type": vehicle_type,
            "direction": direction,
            "confidence": confidence,
            "frame_id": frame_id,
            "timestamp": timestamp,
            "frame": frame,
            "bbox": bbox,
        }
        qsize = self._queue.qsize()

        # Watchdog: warn before full
        if qsize >= _QUEUE_WARN_THRESHOLD and not self._watchdog_logged:
            logger.warning(
                "StorageWorker queue depth %d/%d — backpressure building",
                qsize, self._queue.maxsize,
            )
            self._watchdog_logged = True
        elif qsize < _QUEUE_WARN_THRESHOLD:
            self._watchdog_logged = False

        if qsize >= _QUEUE_CRIT_THRESHOLD:
            logger.error(
                "StorageWorker queue CRITICAL (%d/%d) — dropping event frame_id=%d",
                qsize, self._queue.maxsize, frame_id,
            )
            self.dropped_events += 1
            with self._dead_letter_lock:
                if len(self._dead_letter) < _DEAD_LETTER_MAX:
                    self._dead_letter.append(payload)
            # Emit alert for queue backpressure (only once per threshold crossing)
            if not getattr(self, "_backpressure_alerted", False):
                self._backpressure_alerted = True
                _emit_queue_alert(camera_id, qsize, self._queue.maxsize)
            return

        try:
            self._queue.put_nowait(payload)
        except Full:
            self.dropped_events += 1
            with self._dead_letter_lock:
                if len(self._dead_letter) < _DEAD_LETTER_MAX:
                    self._dead_letter.append(payload)
            logger.warning(
                "StorageWorker queue full — dropping event frame_id=%d "
                "(dropped=%d, qsize=%d)",
                frame_id, self.dropped_events, qsize,
            )

    def stop(self, timeout: float = 10.0) -> None:
        """Signal stop, drain queue, then join."""
        self._stop.set()
        with contextlib.suppress(Exception):
            self._queue.put_nowait(None)  # sentinel
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            logger.warning("StorageWorker did not stop within %.1fs — forcing", timeout)
        if self.publisher is not None:
            self.publisher.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        commit_counter = 0
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except Empty:
                if self._stop.is_set():
                    # Drain remaining items on stop
                    continue
                continue
            if item is None:
                break
            try:
                self._process(item)
                self.total_processed += 1
                commit_counter += 1
                # Batch commit every 50 events — prevents commit-per-event
                # bottleneck (~200 tps → ~200 write-xacts/sec would saturate
                # SQLite WAL at ~100 tps).
                if commit_counter >= 50 and self.adapter is not None:
                    try:
                        self.adapter.events.commit()
                        self.adapter.aggregates.commit()
                    except Exception:
                        logger.warning("StorageWorker: batch commit failed", exc_info=True)
                    commit_counter = 0
            except (OSError, KeyError, ValueError) as exc:
                self.total_errors += 1
                logger.warning(
                    "StorageWorker: error processing event (%s) — dropping",
                    type(exc).__name__,
                )

        # Final commit before draining
        if commit_counter > 0 and self.adapter is not None:
            try:
                self.adapter.events.commit()
                self.adapter.aggregates.commit()
            except Exception:
                logger.warning("StorageWorker: final commit failed", exc_info=True)

        # Drain remaining items
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    try:
                        self._process(item)
                        self.total_processed += 1
                    except (OSError, KeyError, ValueError):
                        self.total_errors += 1
            except Empty:
                break

        # Final commit for any remaining uncommitted rows
        if self.adapter is not None:
            try:
                self.adapter.events.commit()
                self.adapter.aggregates.commit()
            except Exception:
                logger.warning("StorageWorker: drain commit failed", exc_info=True)

        logger.info(
            "StorageWorker stopped: processed=%d, errors=%d, dropped=%d, dead_letter=%d",
            self.total_processed, self.total_errors, self.dropped_events,
            len(self._dead_letter),
        )

    def _process(self, payload: dict[str, Any]) -> None:
        ts: datetime = payload["timestamp"]
        camera_id: str = payload["camera_id"]
        lane_id: str = payload["lane_id"]

        crop_path: str | None = None
        crop_bytes = payload.get("crop_bytes")
        if self.crop_storage is not None and crop_bytes is not None:
            try:
                crop_path = self.crop_storage.save(
                    camera_id=camera_id,
                    lane_id=lane_id,
                    vehicle_type=payload["vehicle_type"],
                    track_id=payload["track_id"],
                    timestamp=ts,
                    crop_bytes=crop_bytes,
                    bbox=payload.get("bbox"),
                )
            except (OSError, ValueError) as exc:
                logger.warning(
                    "StorageWorker: crop save failed (%s) for frame_id=%d",
                    type(exc).__name__, payload["frame_id"],
                )

        if self.adapter is not None:
            try:
                db_breaker.call(self.adapter.events.insert_event, {
                    "camera_id": payload["camera_id"],
                    "job_id": payload["job_id"],
                    "lane_id": payload["lane_id"],
                    "track_id": payload["track_id"],
                    "vehicle_type": payload["vehicle_type"],
                    "direction": payload.get("direction"),
                    "confidence": payload.get("confidence"),
                    "frame_id": payload["frame_id"],
                    "timestamp": payload["timestamp"],
                    "crop_path": crop_path,
                })
            except (CircuitBreakerOpen, OSError, KeyError, ValueError) as exc:
                logger.warning(
                    "StorageWorker: event insert failed (%s) — dead-letter frame_id=%d",
                    type(exc).__name__, payload["frame_id"],
                )
                self._dead_letter.append(payload)
                if len(self._dead_letter) > 10000:
                    self._dead_letter.pop(0)
                return
            self._rollup_aggregates(camera_id, lane_id, payload["vehicle_type"], ts)

        if self.publisher is not None:
            try:
                self.publisher.publish_event(
                    "traffic:events",
                    {
                        "camera_id": camera_id,
                        "job_id": payload["job_id"],
                        "lane_id": lane_id,
                        "track_id": payload["track_id"],
                        "vehicle_type": payload["vehicle_type"],
                        "direction": payload.get("direction"),
                        "confidence": payload.get("confidence"),
                        "frame_id": payload["frame_id"],
                        "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                        "crop_path": crop_path,
                    },
                )
            except (ConnectionError, OSError, ValueError):
                logger.debug("StorageWorker: publish_event failed", exc_info=True)

    def _rollup_aggregates(
        self,
        camera_id: str,
        lane_id: str,
        vehicle_type: str,
        ts: datetime,
    ) -> None:
        if self.adapter is None or self.adapter.aggregates is None:
            return

        buckets = [
            {
                "window": win_name,
                "window_start": WINDOWS[win_name](ts),
                "updated_at": ts,
            }
            for win_name in self.agg_windows
            if win_name in WINDOWS
        ]
        if not buckets:
            return

        try:
            self.adapter.aggregates.upsert_buckets(
                camera_id=camera_id,
                lane_id=lane_id,
                vehicle_type=vehicle_type,
                buckets=buckets,
            )
        except (OSError, KeyError, ValueError) as exc:
            logger.warning(
                "StorageWorker: aggregate UPSERT failed (%s) — event kept but "
                "roll-up counter may be stale",
                type(exc).__name__,
            )
