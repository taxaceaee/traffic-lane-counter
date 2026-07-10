"""Repository protocols — break the reverse ``trafficflow`` → ``trafficflow_server`` dependency.

Background
----------
The AI pipeline (``trafficflow``) historically imported server-side ORM models
directly (``trafficflow_server.db.models``) to write events and aggregates.
That makes the AI package impossible to import in isolation (it drags the
whole server stack with it), breaks test isolation, and inverts the
dependency arrow.

This module defines ``Protocol`` interfaces that describe the *behaviour* the
AI package needs.  The server package provides concrete adapters that
implement these protocols; the AI package depends only on the protocols.

Layout
------
::

    trafficflow/storage/repo_protocol.py   <-- this file
        defines EventRepository / AggregateRepository / CleanupRepository
        AI package imports only this.

    trafficflow_server/storage_adapters.py  <-- server-side adapter
        imports BOTH this protocol AND the server ORM models
        bridges them.

The AI worker constructs the adapter through a small factory:

    from tf_api.storage_adapters import make_server_adapters
    adapter = make_server_adapters(SessionLocal)
    StorageWorker(storage_root=..., adapter=adapter)

When ``adapter`` is ``None`` (file-only mode), the AI package still imports
cleanly — no server dependency loaded.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventRepository(Protocol):
    """Persist a single crossing event row."""

    def insert_event(self, event: dict[str, Any]) -> None:
        """Insert one event row. Implementations must commit on success and
        roll back + raise on failure (callers rely on this for at-least-once
        semantics)."""
        ...


@runtime_checkable
class AggregateRepository(Protocol):
    """Atomically upsert one or more aggregate buckets.

    The implementation MUST be atomic across all rows passed in a single
    call — concurrent workers must not produce unique-constraint violations.
    """

    def upsert_buckets(
        self,
        *,
        camera_id: str,
        lane_id: str,
        vehicle_type: str,
        buckets: list[dict[str, Any]],
    ) -> None:
        """``buckets`` is a list of ``{"window": str, "window_start": datetime,
        "count": int}`` dicts; the implementation increments the count
        field on conflict (or uses count=1 for the first insert)."""
        ...


@runtime_checkable
class LaneChangeRepository(Protocol):
    """Persist a true stable-lane change event."""

    def insert_event(self, event: dict[str, Any]) -> None:
        ...


@runtime_checkable
class CleanupRepository(Protocol):
    """Purge old events / aggregates (used by RetentionCleaner)."""

    def delete_events_before(self, cutoff: datetime) -> int:
        """Delete VehicleCountEvent rows whose ``timestamp`` < cutoff.
        Returns the number of rows deleted."""
        ...

    def delete_aggregates_before(
        self,
        cutoff: datetime,
        window: str,
    ) -> int:
        """Delete TrafficAggregate rows for the given ``window`` whose
        ``window_start`` < cutoff. Returns the number of rows deleted."""
        ...


@runtime_checkable
class RepositoryBundle(Protocol):
    """Composite handle passed to AI-side workers.

    Each attribute may be ``None`` when persistence is disabled (file-only
    mode in tests, offline benchmarks, …).
    """

    events: EventRepository | None
    aggregates: AggregateRepository | None
    lane_changes: LaneChangeRepository | None
    cleanup: CleanupRepository | None

    def close(self) -> None:
        """Release any underlying resources (e.g. session, connection pool)."""
        ...
