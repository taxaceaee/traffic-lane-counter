from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path

from tf_api.api import routes_ws
from tf_common.live_bus import AsyncLiveEventBus, LiveEventBus
from tf_db.models import TrafficAggregate
from tf_db.repositories import SqlAggregateRepository
from tf_db.session import SessionLocal
from tf_worker.storage.storage_worker import StorageWorker


class _FakeCropStorage:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    def save(self, **kwargs):
        self.saved.append(kwargs)
        return f"crops/{kwargs['camera_id']}-{kwargs['track_id']}.jpg"


class _FakeEventsRepo:
    def __init__(self) -> None:
        self.inserted: list[dict] = []
        self.commit_calls = 0

    def insert_event(self, event: dict) -> None:
        self.inserted.append(event)

    def commit(self) -> None:
        self.commit_calls += 1


class _FakeAggregatesRepo:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.commit_calls = 0

    def upsert_buckets(self, **kwargs) -> None:
        self.calls.append(kwargs)

    def commit(self) -> None:
        self.commit_calls += 1


class _FakeAdapter:
    def __init__(self) -> None:
        self.events = _FakeEventsRepo()
        self.aggregates = _FakeAggregatesRepo()


class _FakeLaneChangesRepo:
    def __init__(self) -> None:
        self.inserted: list[dict] = []

    def insert_event(self, event: dict) -> None:
        self.inserted.append(event)


def test_storage_worker_persists_crop_bytes(isolated_runtime, tmp_path: Path):
    crop_storage = _FakeCropStorage()
    adapter = _FakeAdapter()
    worker = StorageWorker(
        storage_root=tmp_path / "storage",
        adapter=adapter,
        crop_storage=crop_storage,
    )
    try:
        worker.enqueue(
            camera_id="CAM_TEST",
            job_id="job-live",
            lane_id="lane_a",
            track_id=7,
            vehicle_type="car",
            direction="forward",
            confidence=0.9,
            frame_id=11,
            timestamp=datetime.now(timezone.utc),
            bbox=[1, 2, 3, 4],
            crop_bytes=b"jpeg-bytes",
        )
    finally:
        worker.stop(timeout=3.0)

    assert len(crop_storage.saved) == 1
    assert crop_storage.saved[0]["crop_bytes"] == b"jpeg-bytes"
    assert adapter.events.inserted[0]["crop_path"] == "crops/CAM_TEST-7.jpg"


def test_storage_worker_persists_lane_change(isolated_runtime, tmp_path: Path):
    adapter = _FakeAdapter()
    adapter.lane_changes = _FakeLaneChangesRepo()
    worker = StorageWorker(storage_root=tmp_path / "storage", adapter=adapter, crop_storage=None)
    try:
        worker.enqueue_lane_change(
            camera_id="CAM_TEST",
            job_id="job-live",
            track_id=7,
            class_name="car",
            previous_lane_id="lane_a",
            current_lane_id="lane_b",
            frame_id=11,
            timestamp=datetime.now(timezone.utc),
        )
    finally:
        worker.stop(timeout=3.0)

    assert adapter.lane_changes.inserted[0]["current_lane_id"] == "lane_b"


def test_sqlite_aggregate_upsert_accumulates_counts(isolated_runtime):
    session = SessionLocal()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    try:
        repo = SqlAggregateRepository(session)
        payload = {
            "camera_id": "CAM_TEST",
            "lane_id": "lane_a",
            "vehicle_type": "car",
            "buckets": [{"window": "1min", "window_start": now, "updated_at": now}],
        }
        repo.upsert_buckets(**payload)
        repo.upsert_buckets(**payload)
        repo.commit()

        row = session.query(TrafficAggregate).filter_by(
            camera_id="CAM_TEST",
            lane_id="lane_a",
            vehicle_type="car",
            window="1min",
            window_start=now,
        ).one()
    finally:
        session.close()

    assert row.count == 2


def test_async_live_event_bus_receives_cross_thread_publish():
    async def _run():
        bus = AsyncLiveEventBus()
        bus.subscribe(["CAM_X"])
        try:
            def _publisher():
                LiveEventBus.publish("CAM_X", {"type": "occupancy_update", "value": 1})

            thread = threading.Thread(target=_publisher)
            thread.start()
            thread.join()

            event = await bus.get(timeout=1.0)
            assert event == ("CAM_X", {"type": "occupancy_update", "value": 1})
        finally:
            bus.close()
            LiveEventBus.clear()

    asyncio.run(_run())


def test_broadcast_raw_matches_exact_camera_subscription():
    class _DummyWebSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send_text(self, message: str) -> None:
            self.messages.append(message)

    async def _run():
        ws_1 = _DummyWebSocket()
        ws_10 = _DummyWebSocket()
        original_connections = routes_ws._connections
        original_ws_cameras = getattr(routes_ws, "_ws_cameras", {})
        try:
            routes_ws._connections = {"CAM_1": [ws_1], "CAM_10": [ws_10]}
            routes_ws._ws_cameras = {id(ws_1): {"CAM_1"}, id(ws_10): {"CAM_10"}}
            await routes_ws.broadcast_raw({"camera_id": "CAM_1", "type": "count_event"})
        finally:
            routes_ws._connections = original_connections
            routes_ws._ws_cameras = original_ws_cameras

        assert len(ws_1.messages) == 1
        assert ws_10.messages == []

    asyncio.run(_run())
