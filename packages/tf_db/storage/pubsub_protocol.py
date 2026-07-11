"""StreamPublisher protocol — real-time event streaming.

Defines the interface for publishing crossing events and occupancy
updates to a real-time channel (Redis Pub/Sub, Kafka, WebSocket fan-out).
The AI pipeline depends only on this protocol; concrete implementations
live in ``trafficflow_server``.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StreamPublisher(Protocol):
    """Publish crossing events and occupancy snapshots in real-time."""

    def publish_event(self, channel: str, data: dict[str, Any]) -> None:
        """Publish a single crossing event to the given channel."""
        ...

    def publish_occupancy(
        self,
        camera_id: str,
        occupancy: dict[str, int],
    ) -> None:
        """Publish per-lane occupancy for a camera."""
        ...

    def close(self) -> None:
        """Release resources (connections, threads)."""
        ...
