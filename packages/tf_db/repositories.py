"""Concrete SQLAlchemy implementations of repo_protocol.py interfaces.

Supports both PostgreSQL (native UPSERT) and SQLite (fallback via
select-then-insert-or-update).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from tf_db.models import (
    AuditLog,
    InferenceJob,
    LaneChangeEvent,
    TrafficAggregate,
    User,
    VehicleCountEvent,
)

logger = logging.getLogger(__name__)

# ── Helpers ─────────────────────────────────────────────────────────────────

def _db_dialect(session: Session) -> str:
    return session.bind.dialect.name if session.bind else "sqlite"

_EVENT_FIELD_MAP = {
    "timestamp": "created_at",
}


def _map_event_fields(event: dict[str, Any]) -> dict[str, Any]:
    """Map legacy field names to ORM column names."""
    mapped = {}
    for k, v in event.items():
        mapped_key = _EVENT_FIELD_MAP.get(k, k)
        # Ensure created_at is a datetime object, not a string
        if mapped_key == "created_at" and isinstance(v, str):
            mapped[mapped_key] = datetime.now(timezone.utc)
        else:
            mapped[mapped_key] = v
    mapped.setdefault("created_at", datetime.now(timezone.utc))
    return mapped


# ── EventRepository ──────────────────────────────────────────────────────────

class SqlEventRepository:
    """Persist crossing events to ``vehicle_count_events`` table.

    Batch commit — call ``flush()`` after each event, ``commit()`` explicitly
    from the caller or via ``flush_and_commit()``.
    """

    def __init__(self, session: Session):
        self.session = session

    def insert_event(self, event: dict[str, Any]) -> None:
        mapped = _map_event_fields(event)
        row = VehicleCountEvent(**mapped)
        self.session.add(row)

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        from tf_db.session import commit_with_retry

        commit_with_retry(self.session)


class SqlJobRepository:
    """Durable inference-job state used by API and worker threads."""

    def __init__(self, session: Session):
        self.session = session

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        row = InferenceJob(**data)
        self.session.add(row)
        self.session.commit()
        return self._serialize(row)

    def update(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        row = self.session.query(InferenceJob).filter(InferenceJob.id == job_id).first()
        if row is None:
            return None
        for key, value in updates.items():
            if hasattr(row, key):
                setattr(row, key, value)
        self.session.commit()
        return self._serialize(row)

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.session.query(InferenceJob).filter(InferenceJob.id == job_id).first()
        return self._serialize(row) if row else None

    def list(self) -> list[dict[str, Any]]:
        rows = self.session.query(InferenceJob).order_by(InferenceJob.created_at.desc()).all()
        return [self._serialize(row) for row in rows]

    @staticmethod
    def _serialize(row: InferenceJob) -> dict[str, Any]:
        return {
            "job_id": row.id,
            "camera_id": row.camera_id,
            "model_id": row.model_id,
            "status": row.status,
            "progress": row.progress,
            "total_frames": row.total_frames,
            "fps": row.fps,
            "ingested_events": row.ingested_events,
            "error": row.error,
            "output_dir": row.output_dir,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }


# ── AggregateRepository ──────────────────────────────────────────────────────

class SqlAggregateRepository:
    """Atomic upsert of aggregate count buckets.

    Uses PostgreSQL ``ON CONFLICT … DO UPDATE`` when available, otherwise
    falls back to a SELECT-then-UPDATE-else-INSERT loop inside a transaction.

    Callers should batch multiple upserts before calling ``commit()``.
    """

    def __init__(self, session: Session):
        self.session = session

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        from tf_db.session import commit_with_retry

        commit_with_retry(self.session)

    def upsert_buckets(
        self,
        *,
        camera_id: str,
        lane_id: str,
        vehicle_type: str,
        buckets: list[dict[str, Any]],
    ) -> None:
        now = datetime.now(timezone.utc)
        dialect = _db_dialect(self.session)

        if dialect == "postgresql":
            self._upsert_pg(camera_id, lane_id, vehicle_type, buckets, now)
        else:
            self._upsert_fallback(camera_id, lane_id, vehicle_type, buckets, now)

    def _upsert_pg(
        self, camera_id: str, lane_id: str, vehicle_type: str,
        buckets: list[dict[str, Any]], now: datetime,
    ) -> None:
        for b in buckets:
            stmt = pg_insert(TrafficAggregate).values(
                camera_id=camera_id,
                lane_id=lane_id,
                vehicle_type=vehicle_type,
                window=b["window"],
                window_start=b["window_start"],
                count=1,
                updated_at=now,
            ).on_conflict_do_update(
                constraint="uq_aggregate_key",
                set_={
                    "count": TrafficAggregate.count + 1,
                    "updated_at": now,
                },
            )
            self.session.execute(stmt)
        # Single commit for all buckets — avoids N+1 commit per event

    def _upsert_fallback(
        self, camera_id: str, lane_id: str, vehicle_type: str,
        buckets: list[dict[str, Any]], now: datetime,
    ) -> None:
        dialect = _db_dialect(self.session)
        if dialect == "sqlite":
            for b in buckets:
                stmt = sqlite_insert(TrafficAggregate).values(
                    camera_id=camera_id,
                    lane_id=lane_id,
                    vehicle_type=vehicle_type,
                    window=b["window"],
                    window_start=b["window_start"],
                    count=1,
                    updated_at=now,
                ).on_conflict_do_update(
                    index_elements=[
                        TrafficAggregate.camera_id,
                        TrafficAggregate.lane_id,
                        TrafficAggregate.vehicle_type,
                        TrafficAggregate.window,
                        TrafficAggregate.window_start,
                    ],
                    set_={
                        "count": TrafficAggregate.count + 1,
                        "updated_at": now,
                    },
                )
                self.session.execute(stmt)
            return

        for b in buckets:
            row = self.session.query(TrafficAggregate).filter_by(
                camera_id=camera_id,
                lane_id=lane_id,
                vehicle_type=vehicle_type,
                window=b["window"],
                window_start=b["window_start"],
            ).with_for_update().first()
            if row is None:
                self.session.add(TrafficAggregate(
                    camera_id=camera_id,
                    lane_id=lane_id,
                    vehicle_type=vehicle_type,
                    window=b["window"],
                    window_start=b["window_start"],
                    count=1,
                    updated_at=now,
                ))
            else:
                row.count += 1
                row.updated_at = now
        # Single commit for all buckets


# ── QueryRepository — read queries for dashboard / API ─────────────────────────

# Explicit demo/seed job markers — never surface as fleet traffic by default.
DEMO_JOB_IDS = frozenset({"seed", "demo", "sample", "fixture"})


class SqlQueryRepository:
    """Read-only queries on top of vehicle_count_events + traffic_aggregates.

    By default, rows with ``job_id`` in :data:`DEMO_JOB_IDS` are excluded so
    ``scripts.seed_db`` never masquerades as live pipeline traffic.
    """

    def __init__(self, session: Session, *, exclude_demo_jobs: bool = True):
        self.session = session
        self.exclude_demo_jobs = exclude_demo_jobs

    def _filter_demo_jobs(self, q):
        if not self.exclude_demo_jobs:
            return q
        # Keep NULL job_ids and all real jobs (live-*, offline UUIDs, …).
        return q.filter(
            (VehicleCountEvent.job_id.is_(None))
            | (~VehicleCountEvent.job_id.in_(tuple(DEMO_JOB_IDS)))
        )

    def get_counts_summary(
        self,
        camera_id: str | None = None,
        lane_id: str | None = None,
        vehicle_type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        """Return per-lane per-vehicle-type counts matching filters."""
        from datetime import timedelta

        from sqlalchemy import func

        q = self.session.query(
            VehicleCountEvent.camera_id,
            VehicleCountEvent.lane_id,
            VehicleCountEvent.vehicle_type,
            func.count().label("count"),
        )
        q = self._filter_demo_jobs(q)

        if camera_id:
            q = q.filter(VehicleCountEvent.camera_id == camera_id)
        if lane_id:
            q = q.filter(VehicleCountEvent.lane_id == lane_id)
        if vehicle_type:
            q = q.filter(VehicleCountEvent.vehicle_type == vehicle_type)
        if since:
            q = q.filter(VehicleCountEvent.created_at >= since)
        if until:
            q = q.filter(VehicleCountEvent.created_at <= until)
        else:
            # Default: last 24h
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            q = q.filter(VehicleCountEvent.created_at >= cutoff)

        q = q.group_by(
            VehicleCountEvent.camera_id,
            VehicleCountEvent.lane_id,
            VehicleCountEvent.vehicle_type,
        )
        rows = q.all()
        return [
            {
                "camera_id": r.camera_id,
                "lane_id": r.lane_id,
                "vehicle_type": r.vehicle_type,
                "count": r.count,
            }
            for r in rows
        ]

    def get_counts_total(
        self,
        camera_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> int:
        """Return total vehicle count matching filters."""
        from datetime import timedelta

        from sqlalchemy import func

        q = self.session.query(func.count(VehicleCountEvent.id))
        q = self._filter_demo_jobs(q)
        if camera_id:
            q = q.filter(VehicleCountEvent.camera_id == camera_id)
        if since:
            q = q.filter(VehicleCountEvent.created_at >= since)
        if until:
            q = q.filter(VehicleCountEvent.created_at <= until)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            q = q.filter(VehicleCountEvent.created_at >= cutoff)
        return q.scalar() or 0

    def get_lane_changes(
        self,
        camera_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Return true stable-lane changes, newest first."""
        return SqlLaneChangeRepository(self.session).get_events(
            camera_id, limit=limit, offset=offset
        )

    def get_recent_events(
        self,
        camera_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Return the most recent raw count events for a camera."""
        q = self.session.query(VehicleCountEvent).filter(
            VehicleCountEvent.camera_id == camera_id,
        )
        q = self._filter_demo_jobs(q)
        q = q.order_by(VehicleCountEvent.created_at.desc()).limit(limit).offset(offset)

        return [
            {
                "id": ev.id,
                "camera_id": ev.camera_id,
                "job_id": ev.job_id,
                "lane_id": ev.lane_id,
                "track_id": ev.track_id,
                "vehicle_type": ev.vehicle_type,
                "direction": ev.direction,
                "confidence": ev.confidence,
                "frame_id": ev.frame_id,
                "timestamp": ev.created_at.isoformat() if ev.created_at else None,
                "crop_path": ev.crop_path,
            }
            for ev in q.all()
        ]

    def get_occupancy_history(
        self,
        camera_id: str,
        limit: int = 500,
        window: str = "1min",
    ) -> list[dict]:
        """Return occupancy time-series from aggregates table."""
        q = self.session.query(
            TrafficAggregate.window_start,
            TrafficAggregate.lane_id,
            TrafficAggregate.vehicle_type,
            TrafficAggregate.count,
        ).filter(
            TrafficAggregate.camera_id == camera_id,
            TrafficAggregate.window == window,
        ).order_by(TrafficAggregate.window_start.desc()).limit(limit)

        return [
            {
                "timestamp": r.window_start.isoformat() if r.window_start else None,
                "lane_id": r.lane_id,
                "vehicle_type": r.vehicle_type,
                "count": r.count,
            }
            for r in q.all()
        ]

    def get_direction_summary(
        self,
        camera_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        """Return per-lane direction counts (forward/backward) from events table.

        Uses SQL aggregation (GROUP BY) for efficiency — O(1 row per lane*direction)
        instead of scanning 100k+ individual event rows.
        """
        from datetime import timedelta

        from sqlalchemy import func

        q = self.session.query(
            VehicleCountEvent.lane_id,
            VehicleCountEvent.direction,
            func.count().label("count"),
        ).filter(
            VehicleCountEvent.camera_id == camera_id,
            VehicleCountEvent.direction.isnot(None),
            VehicleCountEvent.direction != "",
            VehicleCountEvent.direction.in_(["forward", "backward"]),
        )
        q = self._filter_demo_jobs(q)

        if since:
            q = q.filter(VehicleCountEvent.created_at >= since)
        if until:
            q = q.filter(VehicleCountEvent.created_at <= until)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            q = q.filter(VehicleCountEvent.created_at >= cutoff)

        q = q.group_by(VehicleCountEvent.lane_id, VehicleCountEvent.direction)
        rows = q.all()
        return [
            {"lane_id": r.lane_id, "direction": r.direction, "count": r.count}
            for r in rows
        ]

    def get_latest_occupancy(self, camera_id: str) -> dict[str, int]:
        """Return latest per-lane vehicle count based on recent events."""
        from datetime import timedelta

        from sqlalchemy import func

        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        q = self.session.query(
            VehicleCountEvent.lane_id,
            func.count(func.distinct(VehicleCountEvent.track_id)).label("vehicle_count"),
        ).filter(
            VehicleCountEvent.camera_id == camera_id,
            VehicleCountEvent.created_at >= recent,
        )
        q = self._filter_demo_jobs(q)
        q = q.group_by(VehicleCountEvent.lane_id)
        occ: dict[str, int] = {}
        for lane_id, vehicle_count in q:
            occ[lane_id] = int(vehicle_count or 0)
        return occ if occ else {"no_recent_data": 0}

    def get_counts_timeseries(
        self,
        camera_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        window: str = "1hour",
        limit: int = 168,
    ) -> list[dict]:
        """Return aggregated count time-series from traffic_aggregates table."""
        from datetime import timedelta

        from tf_db.models import TrafficAggregate

        q = self.session.query(
            TrafficAggregate.window_start,
            TrafficAggregate.lane_id,
            TrafficAggregate.vehicle_type,
            TrafficAggregate.count,
        ).filter(
            TrafficAggregate.window == window,
        )

        if camera_id:
            q = q.filter(TrafficAggregate.camera_id == camera_id)

        if since:
            q = q.filter(TrafficAggregate.window_start >= since)
        if until:
            q = q.filter(TrafficAggregate.window_start <= until)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            q = q.filter(TrafficAggregate.window_start >= cutoff)

        q = q.order_by(TrafficAggregate.window_start.desc()).limit(limit)

        return [
            {
                "timestamp": r.window_start.isoformat() if r.window_start else None,
                "lane_id": r.lane_id,
                "vehicle_type": r.vehicle_type,
                "count": r.count,
            }
            for r in q.all()
        ]

    def get_counts_hourly_from_events(
        self,
        camera_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        """Bucket raw crossing events by hour (fallback when aggregates are empty)."""
        from datetime import timedelta

        from sqlalchemy import Integer, cast, func

        if until is None:
            until = datetime.now(timezone.utc)
        if since is None:
            since = until - timedelta(hours=24)

        bind = self.session.get_bind()
        dialect = getattr(getattr(bind, "dialect", None), "name", "sqlite") or "sqlite"
        if dialect == "sqlite":
            hour_expr = cast(func.strftime("%H", VehicleCountEvent.created_at), Integer)
        else:
            hour_expr = func.extract("hour", VehicleCountEvent.created_at)

        q = self.session.query(
            hour_expr.label("hour"),
            func.count(VehicleCountEvent.id).label("count"),
        ).filter(
            VehicleCountEvent.created_at >= since,
            VehicleCountEvent.created_at < until,
        )
        q = self._filter_demo_jobs(q)
        if camera_id:
            q = q.filter(VehicleCountEvent.camera_id == camera_id)
        q = q.group_by(hour_expr)

        return [
            {"hour": int(r.hour), "count": int(r.count or 0)}
            for r in q.all()
            if r.hour is not None
        ]


# ── LaneChangeRepository ────────────────────────────────────────────────────

class SqlLaneChangeRepository:
    """Persist lane-change events to ``lane_change_events`` table."""

    def __init__(self, session: Session):
        self.session = session

    def insert_event(self, event: dict[str, Any]) -> None:
        row = LaneChangeEvent(**event)
        self.session.add(row)

    def get_events(
        self,
        camera_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        q = self.session.query(LaneChangeEvent).filter(
            LaneChangeEvent.camera_id == camera_id,
        ).order_by(LaneChangeEvent.created_at.desc()).limit(limit).offset(offset)

        return [
            {
                "id": ev.id,
                "camera_id": ev.camera_id,
                "track_id": ev.track_id,
                "class_name": ev.class_name,
                "previous_lane_id": ev.previous_lane_id,
                "current_lane_id": ev.current_lane_id,
                "frame_id": ev.frame_id,
                "timestamp": ev.created_at.isoformat() if ev.created_at else None,
            }
            for ev in q.all()
        ]

    def commit(self) -> None:
        self.session.commit()

    def flush(self) -> None:
        self.session.flush()


# ── MetricsRepository ──────────────────────────────────────────────────────────

class SqlMetricsRepository:
    """Query runtime metrics with time-windowing to avoid full-table scans."""

    def __init__(self, session: Session):
        self.session = session

    def get_avg_fps(self, camera_id: str | None = None, window_hours: int = 24) -> float | None:
        from datetime import timedelta

        from tf_db.models import RuntimeMetric
        q = self.session.query(RuntimeMetric.fps)
        if camera_id:
            q = q.filter(RuntimeMetric.camera_id == camera_id)
        cutoff = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=window_hours)
        q = q.filter(RuntimeMetric.created_at >= cutoff)
        rows = q.all()
        if not rows:
            return None
        vals = [r.fps for r in rows if r.fps is not None]
        return sum(vals) / len(vals) if vals else None

    def get_avg_latency(self, camera_id: str | None = None, window_hours: int = 24) -> float | None:
        from datetime import timedelta

        from tf_db.models import RuntimeMetric
        q = self.session.query(RuntimeMetric.latency_ms)
        if camera_id:
            q = q.filter(RuntimeMetric.camera_id == camera_id)
        cutoff = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=window_hours)
        q = q.filter(RuntimeMetric.created_at >= cutoff)
        rows = q.all()
        if not rows:
            return None
        vals = [r.latency_ms for r in rows if r.latency_ms is not None]
        return sum(vals) / len(vals) if vals else None


# ── UserRepository ───────────────────────────────────────────────────────────

class SqlUserRepository:
    """User CRUD against the ``users`` table."""

    def __init__(self, session: Session):
        self.session = session

    def list_users(self) -> list[dict]:
        rows = self.session.query(User).order_by(User.created_at.desc()).all()
        return [self._serialize(u) for u in rows]

    def get_by_id(self, user_id: str) -> dict | None:
        u = self.session.query(User).filter(User.id == user_id).first()
        return self._serialize(u) if u else None

    def get_by_username(self, username: str) -> dict | None:
        u = self.session.query(User).filter(User.username == username).first()
        return self._serialize(u) if u else None

    def get_user_model(self, username: str):  # returns ORM object for auth
        return self.session.query(User).filter(User.username == username).first()

    def create(self, user_data: dict) -> dict:
        u = User(**user_data)
        self.session.add(u)
        self.session.commit()
        return self._serialize(u)

    def update(self, user_id: str, updates: dict) -> dict | None:
        u = self.session.query(User).filter(User.id == user_id).first()
        if not u:
            return None
        for k, v in updates.items():
            if v is not None and hasattr(u, k):
                setattr(u, k, v)
        self.session.commit()
        return self._serialize(u)

    def update_last_login(self, username: str, dt: datetime) -> None:
        u = self.session.query(User).filter(User.username == username).first()
        if u:
            u.last_login = dt
            self.session.commit()

    def revoke_tokens(self, username: str, dt: datetime) -> bool:
        """Revoke all tokens issued before ``dt`` for one user."""
        u = self.session.query(User).filter(User.username == username).first()
        if not u:
            return False
        u.token_version = (u.token_version or 0) + 1
        self.session.commit()
        return True

    def _serialize(self, u) -> dict:
        return {
            "id": u.id,
            "username": u.username,
            "email": u.email or "",
            "role": u.role,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "token_version": u.token_version or 0,
        }


# ── AuditLogRepository ───────────────────────────────────────────────────────

class SqlAuditRepository:
    """Write and read audit log entries."""

    def __init__(self, session: Session):
        self.session = session

    def add_entry(self, entry: dict) -> dict:
        log = AuditLog(**entry)
        self.session.add(log)
        self.session.commit()
        return self._serialize(log)

    def list_entries(self, limit: int = 50,
                     user_id: str | None = None,
                     action: str | None = None) -> list[dict]:
        q = self.session.query(AuditLog)
        if user_id:
            q = q.filter(AuditLog.user_id == user_id)
        if action:
            q = q.filter(AuditLog.action == action)
        q = q.order_by(AuditLog.created_at.desc()).limit(limit)
        return [self._serialize(e) for e in q.all()]

    def _serialize(self, e) -> dict:
        return {
            "id": e.id,
            "user_id": e.user_id,
            "username": e.username,
            "action": e.action,
            "resource": e.resource or "",
            "detail": e.detail or "",
            "ip_address": e.ip_address or "",
            "timestamp": e.created_at.isoformat() if e.created_at else None,
        }
