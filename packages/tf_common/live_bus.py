"""LiveEventBus — in-process pub/sub for live events when Redis is not available.

Guarantees:
- Thread-safe publish/subscribe
- No external dependencies
- Async-compatible (subscribers receive events via asyncio futures)
- Works alongside Redis: when Redis is available, events flow through both
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any, ClassVar

logger = logging.getLogger("trafficflow.live_bus")


class LiveEventBus:
    """In-process pub/sub for live detection events.

    Usage (publisher side — pipeline thread)::

        LiveEventBus.publish("CAM_01", {"type": "occupancy_update", "data": {...}})

    Usage (subscriber side — async WebSocket handler)::

        def handler(camera_id, data):
            ...

        LiveEventBus.subscribe("CAM_01", handler)
    """

    _subscribers: ClassVar[dict[str, list[Callable[[str, dict[str, Any]], None]]]] = {}
    _lock = threading.Lock()

    @classmethod
    def publish(cls, camera_id: str, data: dict[str, Any]) -> None:
        """Publish an event for a camera to all in-process subscribers.

        Exceptions from handlers are caught so a slow/blocking subscriber
        never crashes the pipeline thread.
        """
        with cls._lock:
            handlers = list(cls._subscribers.get(camera_id, []))
            global_handlers = list(cls._subscribers.get("*", []))

        for handler in handlers:
            try:
                handler(camera_id, data)
            except Exception:
                logger.warning("LiveEventBus: handler failed for %s", camera_id, exc_info=True)

        for handler in global_handlers:
            try:
                handler(camera_id, data)
            except Exception:
                logger.warning("LiveEventBus: global handler failed", exc_info=True)

    @classmethod
    def subscribe(cls, camera_id: str, handler: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a handler for events from a specific camera (or '*' for all)."""
        with cls._lock:
            cls._subscribers.setdefault(camera_id, []).append(handler)

    @classmethod
    def unsubscribe(cls, camera_id: str, handler: Callable) -> None:
        """Remove a previously registered handler."""
        with cls._lock:
            handlers = cls._subscribers.get(camera_id, [])
            if handler in handlers:
                handlers.remove(handler)
            if not handlers:
                cls._subscribers.pop(camera_id, None)

    @classmethod
    def clear(cls) -> None:
        """Remove all subscribers (use during shutdown)."""
        with cls._lock:
            cls._subscribers.clear()


# ── Async bridge: creates asyncio-friendly subscriber ──────────────────────

class AsyncLiveEventBus:
    """Async wrapper that accumulates events into a queue for WebSocket send."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=256)
        self._camera_ids: list[str] = []
        self._subscribed: bool = False

    def _handler(self, camera_id: str, data: dict[str, Any]) -> None:
        """Callback from LiveEventBus — push to async queue, drop oldest on overflow."""
        if self._camera_ids and camera_id not in self._camera_ids:
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue_from_thread, camera_id, data)
        except RuntimeError:
            logger.debug("AsyncLiveEventBus loop is closed")

    def _enqueue_from_thread(self, camera_id: str, data: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((camera_id, data))
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((camera_id, data))
            except (asyncio.QueueFull, asyncio.QueueEmpty):
                logger.debug("AsyncLiveEventBus queue is full while dropping an event")

    def subscribe(self, camera_ids: list[str] | None = None) -> None:
        """Start listening for specific cameras (None = all)."""
        if self._subscribed:
            return
        self._subscribed = True
        self._camera_ids = camera_ids or []
        LiveEventBus.subscribe("*", self._handler)

    def unsubscribe(self) -> None:
        """Stop listening."""
        if not self._subscribed:
            return
        self._subscribed = False
        LiveEventBus.unsubscribe("*", self._handler)

    async def get(self, timeout: float = 1.0) -> tuple[str, dict[str, Any]] | None:
        """Get next event with timeout. Returns (camera_id, data) or None."""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self.unsubscribe()
