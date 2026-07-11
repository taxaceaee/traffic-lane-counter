"""Counting API — operational line-crossing counts + live session context.

Provides:
- GET /api/cameras/{camera_id}/counts/summary   — totals, directions, readiness, live
- GET /api/cameras/{camera_id}/counts/timeseries — hourly buckets for filter window
- GET /api/cameras/{camera_id}/counts/recent     — last N crossing events

All endpoints fall back to empty/zero results when the DB has no data.
Live session fields come from always-on pipelines (unique tracks, not line counts).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query

from tf_api.api.routes_auth import get_current_user
from tf_common.safe_path import validate_identifier
from tf_db.repositories import SqlQueryRepository
from tf_db.session import SessionLocal

logger = logging.getLogger("trafficflow.counts")

router = APIRouter(prefix="/api/cameras", tags=["counting"])


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _lane_readiness(camera_id: str) -> dict[str, Any]:
    """Inspect lane YAML — track-based counting needs polygons, not tripwires."""
    path = Path("configs/lanes") / f"{camera_id}_lanes.yaml"
    if not path.exists():
        return {
            "lanes_configured": 0,
            "lanes_with_polygon": 0,
            "count_mode": "track_lane",
            "ready": False,
            "config_path": str(path),
        }
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.debug("Failed reading lanes for readiness %s", camera_id, exc_info=True)
        return {
            "lanes_configured": 0,
            "lanes_with_polygon": 0,
            "count_mode": "track_lane",
            "ready": False,
            "config_path": str(path),
        }
    lanes = raw.get("lanes") or []
    with_poly = 0
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        poly = lane.get("polygon") or lane.get("points") or []
        if isinstance(poly, list) and len(poly) >= 3:
            with_poly += 1
    total = len([l for l in lanes if isinstance(l, dict)])
    return {
        "lanes_configured": total,
        "lanes_with_polygon": with_poly,
        # Legacy keys kept so older SPA builds don't break.
        "lanes_with_counting_line": 0,
        "lanes_missing_line": [],
        "count_mode": "track_lane",
        "ready": with_poly > 0,
        "config_path": str(path),
    }


def _live_pipeline_snapshot(camera_id: str) -> dict[str, Any]:
    """Realtime session tallies from always-on pipeline (not line crossings)."""
    empty = {
        "pipeline_running": False,
        "always_on": False,
        "live": False,
        "status": "stopped",
        "process_fps": 0.0,
        "source_fps": 0.0,
        "avg_latency_ms": 0.0,
        "occupancy": {},
        "occupancy_total": 0,
        "vehicle_types": {},
        "session_tracks": 0,
        "viewers": 0,
        "error": None,
    }
    try:
        from tf_api.api.routes_live import _lock, _streams
        from tf_common.monitoring.live_metrics import get_camera_metrics
    except Exception:
        return empty

    with _lock:
        stream = _streams.get(camera_id)

    if stream is None:
        return empty

    meta = stream[2]
    metrics = get_camera_metrics(camera_id)
    vtypes = dict(meta.get("vehicle_types") or {})
    occ = dict(meta.get("occupancy") or {})
    occ_total = 0
    for v in occ.values():
        try:
            n = int(v)
            if n > 0:
                occ_total += n
        except (TypeError, ValueError):
            continue
    session_tracks = 0
    for v in vtypes.values():
        try:
            n = int(v)
            if n > 0:
                session_tracks += n
        except (TypeError, ValueError):
            continue
    connections = int(meta.get("connections") or 0)
    return {
        "pipeline_running": True,
        "always_on": bool(meta.get("always_on")),
        "live": bool(metrics.get("stream_active")) or connections > 0,
        "status": metrics.get("status") or "active",
        "process_fps": float(metrics.get("process_fps") or 0.0),
        "source_fps": float(metrics.get("source_fps") or 0.0),
        "avg_latency_ms": float(metrics.get("avg_latency_ms") or 0.0),
        "occupancy": occ,
        "occupancy_total": occ_total,
        "vehicle_types": vtypes,
        "session_tracks": session_tracks,
        "viewers": connections,
        "error": metrics.get("error"),
    }


def _window_hours(since: datetime | None, until: datetime | None) -> float:
    end = until or datetime.now(timezone.utc)
    start = since
    if start is None:
        start = end - timedelta(hours=24)
    # Normalize naive → UTC assume
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    secs = max((end - start).total_seconds(), 1.0)
    return secs / 3600.0


@router.get("/{camera_id}/counts/summary")
async def get_counts_summary(
    camera_id: str,
    since: str | None = Query(None, description="ISO datetime for start of range"),
    until: str | None = Query(None, description="ISO datetime for end of range"),
    _user: dict = Depends(get_current_user),
):
    """Return per-lane counts + direction split + readiness + live session context."""
    validate_identifier(camera_id, name="camera_id")

    since_dt = _parse_dt(since)
    until_dt = _parse_dt(until)

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)
        rows = repo.get_counts_summary(
            camera_id=camera_id,
            since=since_dt,
            until=until_dt,
        )
        total = repo.get_counts_total(
            camera_id=camera_id,
            since=since_dt,
            until=until_dt,
        )
        directions = repo.get_direction_summary(
            camera_id=camera_id,
            since=since_dt,
            until=until_dt,
        )
        # Latest crossings for "last event at" + rate pulse
        recent = repo.get_recent_events(camera_id, limit=5)
    except Exception:
        logger.error("Failed to query counts summary for %s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema") from None
    finally:
        session.close()

    lanes_map: dict[str, dict[str, int]] = {}
    for r in rows:
        lane = r["lane_id"]
        vtype = r["vehicle_type"]
        count = r["count"]
        if lane not in lanes_map:
            lanes_map[lane] = {}
        lanes_map[lane][vtype] = lanes_map[lane].get(vtype, 0) + count

    dir_by_lane: dict[str, dict[str, int]] = {}
    forward = backward = 0
    for d in directions:
        lid = d["lane_id"]
        direction = str(d.get("direction") or "").lower()
        cnt = int(d.get("count") or 0)
        bucket = dir_by_lane.setdefault(lid, {"forward": 0, "backward": 0})
        if direction in bucket:
            bucket[direction] += cnt
        if direction == "forward":
            forward += cnt
        elif direction == "backward":
            backward += cnt

    lanes_out = []
    type_totals: dict[str, int] = {}
    for lane_id, vtypes in lanes_map.items():
        for t, c in vtypes.items():
            type_totals[t] = type_totals.get(t, 0) + int(c)
        entry = {
            "lane_id": lane_id,
            "types": vtypes,
            "total": sum(vtypes.values()),
            "directions": dir_by_lane.get(lane_id, {"forward": 0, "backward": 0}),
        }
        lanes_out.append(entry)

    # Include lanes that only appear in direction summary / readiness
    for lid, dmap in dir_by_lane.items():
        if lid not in lanes_map:
            lanes_out.append({
                "lane_id": lid,
                "types": {},
                "total": int(dmap.get("forward", 0)) + int(dmap.get("backward", 0)),
                "directions": dmap,
            })

    hours = _window_hours(since_dt, until_dt)
    rate_per_hour = round(float(total) / hours, 2) if hours > 0 else 0.0

    last_event_at = None
    if recent:
        last_event_at = recent[0].get("timestamp")

    readiness = _lane_readiness(camera_id)
    live = _live_pipeline_snapshot(camera_id)

    return {
        "camera_id": camera_id,
        "total": total,
        "lanes": lanes_out,
        "type_totals": type_totals,
        "directions": {
            "forward": forward,
            "backward": backward,
            "by_lane": dir_by_lane,
        },
        "rate_per_hour": rate_per_hour,
        "window_hours": round(hours, 3),
        "last_event_at": last_event_at,
        "readiness": readiness,
        "live": live,
        "data_sources": {
            "track_lane_counts": "db",
            "line_crossings": "db",  # legacy alias — same DB table, track-based events
            "session_tracks": "live_pipeline",
            "count_mode": "track_lane",
        },
        "since": since_dt.isoformat() if since_dt else None,
        "until": until_dt.isoformat() if until_dt else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/{camera_id}/counts/timeseries")
async def get_counts_timeseries(
    camera_id: str,
    window: str = Query("1hour", pattern="^(1min|5min|1hour|1day)$"),
    since: str | None = Query(None),
    until: str | None = Query(None),
    limit: int = Query(168, ge=1, le=1000),
    _user: dict = Depends(get_current_user),
):
    """Return count time-series (aggregates, fallback to raw event hourly buckets)."""
    validate_identifier(camera_id, name="camera_id")
    since_dt = _parse_dt(since)
    until_dt = _parse_dt(until)

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)
        rows = repo.get_counts_timeseries(
            camera_id=camera_id,
            since=since_dt,
            until=until_dt,
            window=window if window in ("1hour", "1day", "5min", "1min") else "1hour",
            limit=limit,
        )
        source = "aggregates"
        # Collapse to timestamp -> count for chart simplicity
        buckets: dict[str, int] = {}
        for r in rows:
            ts = str(r.get("timestamp") or "")
            if not ts:
                continue
            buckets[ts] = buckets.get(ts, 0) + int(r.get("count") or 0)

        if not buckets and window in ("1hour", "1day"):
            # Fallback: hourly from raw events for today / range
            end = until_dt or datetime.now(timezone.utc)
            start = since_dt or (end - timedelta(hours=24))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            hourly = repo.get_counts_hourly_from_events(
                camera_id=camera_id,
                since=start,
                until=end,
            )
            # Map hour-of-day onto today's date labels for the chart
            day = start.date()
            for hrow in hourly:
                h = int(hrow.get("hour", 0))
                ts = datetime(day.year, day.month, day.day, h, tzinfo=timezone.utc)
                key = ts.isoformat()
                buckets[key] = buckets.get(key, 0) + int(hrow.get("count") or 0)
            source = "events"
    except Exception:
        logger.error("Failed to query timeseries for %s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema") from None
    finally:
        session.close()

    data = [
        {"timestamp": ts, "count": cnt}
        for ts, cnt in sorted(buckets.items(), key=lambda x: x[0])
    ]
    return {
        "camera_id": camera_id,
        "window": window,
        "source": source,
        "data": data,
    }


@router.get("/{camera_id}/counts/recent")
async def get_counts_recent(
    camera_id: str,
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    """Return the most recent track-count / crossing events for a camera."""
    validate_identifier(camera_id, name="camera_id")

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)
        events = repo.get_recent_events(camera_id, limit=limit)
    except Exception:
        logger.error("Failed to query recent counts for %s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema") from None
    finally:
        session.close()

    return {
        "camera_id": camera_id,
        "events": events,
        "count": len(events),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
