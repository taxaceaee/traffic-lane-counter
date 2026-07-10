"""RedisPublisher — publishes AI inference results to Redis Pub/Sub channels.

Channels
--------
traffic:events      — one message per vehicle crossing event (JSON)
traffic:live:{cam}  — periodic live state snapshot per camera (JSON)

Production hardening:
- Shared connection pool across all RedisPublisher instances
- Circuit breaker to prevent cascade failure
- Serialization via orjson (3-6x faster)
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Any

from backend.serializer import dumps
from backend.services.circuit_breaker import redis_breaker

logger = logging.getLogger("trafficflow.pubsub")

# Shared Redis connection pool — prevents N TCP connections for N cameras
_connection_pool: Any = None
_pool_lock = threading.Lock()


def _get_pool() -> Any:
    global _connection_pool
    if _connection_pool is None:
        with _pool_lock:
            if _connection_pool is None:
                import redis
                _connection_pool = redis.ConnectionPool(
                    host=os.getenv("REDIS_HOST", "localhost"),
                    port=int(os.getenv("REDIS_PORT", 6379)),
                    db=0,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_keepalive=True,
                    max_connections=20,
                )
    return _connection_pool


class RedisPublisher:
    """Thin wrapper around redis.Redis for publishing inference results.

    Shares a single connection pool across all instances — at 50 cameras
    this means ~2-3 actual TCP connections instead of 50.
    """

    CHANNEL_EVENTS = "traffic:events"
    CHANNEL_LIVE_TMPL = "traffic:live:{camera_id}"

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        db: int = 0,
    ):
        self._host = host or os.getenv("REDIS_HOST", "localhost")
        self._port = int(port or os.getenv("REDIS_PORT", 6379))
        self._db = db
        self._redis = None

    def _get_redis(self):
        if self._redis is None:
            import redis
            self._redis = redis.Redis(connection_pool=_get_pool())
        return self._redis

    def publish_event(self, channel: str, data: dict[str, Any]) -> None:
        """Publish a single vehicle crossing event to the given channel."""
        try:
            redis_breaker.call(self._get_redis().publish, channel, dumps(data))
        except (ConnectionError, OSError, ValueError):
            logger.warning("RedisPublisher: failed to publish event", exc_info=True)

    def publish_live_state(self, camera_id: str, state: dict[str, Any]) -> None:
        """Publish a live occupancy/count snapshot for one camera."""
        channel = self.CHANNEL_LIVE_TMPL.format(camera_id=camera_id)
        try:
            redis_breaker.call(self._get_redis().publish, channel, dumps(state))
        except (ConnectionError, OSError, ValueError):
            logger.warning("RedisPublisher: failed to publish live state", exc_info=True)

    def publish_occupancy(self, camera_id: str, occupancy: dict[str, int]) -> None:
        """Publish per-lane occupancy for a camera."""
        self.publish_live_state(camera_id, {"occupancy": occupancy})

    def close(self) -> None:
        if self._redis is not None:
            try:
                self._redis.close()
            except (ConnectionError, OSError):
                logger.warning("RedisPublisher: close failed", exc_info=True)
            self._redis = None
