"""Counting API — real vehicle counting data from database queries.

Provides:
- GET /api/cameras/{camera_id}/counts/summary   — totals per lane / vehicle type
- GET /api/cameras/{camera_id}/counts/timeseries — hourly/daily aggregations
- GET /api/cameras/{camera_id}/counts/recent     — last N crossing events

All endpoints fall back to empty/zero results when the DB has no data.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from backend.db.repositories import SqlQueryRepository
from backend.db.session import SessionLocal
from backend.io.safe_path import validate_identifier

logger = logging.getLogger("trafficflow.counts")

router = APIRouter(prefix="/api/cameras", tags=["counting"])


@router.get("/{camera_id}/counts/summary")
async def get_counts_summary(
    camera_id: str,
    since: str | None = Query(None, description="ISO datetime for start of range"),
    until: str | None = Query(None, description="ISO datetime for end of range"),
):
    """Return per-lane per-vehicle-type counts for a camera.

    By default queries the last 24h.  Pass ``since``/``until`` as ISO-8601
    strings to customise the range (e.g. ``since=2026-07-01T00:00:00Z``).
    """
    validate_identifier(camera_id, name="camera_id")

    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

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
    except Exception:
        logger.error("Failed to query counts summary for %s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema")
    finally:
        session.close()

    # Aggregate per lane
    lanes_map: dict[str, dict[str, int]] = {}
    for r in rows:
        lane = r["lane_id"]
        vtype = r["vehicle_type"]
        count = r["count"]
        if lane not in lanes_map:
            lanes_map[lane] = {}
        lanes_map[lane][vtype] = lanes_map[lane].get(vtype, 0) + count

    lanes_out = []
    for lane_id, vtypes in lanes_map.items():
        entry = {"lane_id": lane_id, "types": vtypes, "total": sum(vtypes.values())}
        lanes_out.append(entry)

    return {
        "camera_id": camera_id,
        "total": total,
        "lanes": lanes_out,
    }


@router.get("/{camera_id}/counts/timeseries")
async def get_counts_timeseries(
    camera_id: str,
    window: str = Query("1hour", pattern="^(1min|5min|1hour|1day)$"),
    limit: int = Query(168, ge=1, le=1000),
):
    """Return aggregated time-series of vehicle counts."""
    validate_identifier(camera_id, name="camera_id")

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)
        rows = repo.get_occupancy_history(
            camera_id=camera_id,
            limit=limit,
            window=window,
        )
    except Exception:
        logger.error("Failed to query timeseries for %s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema")
    finally:
        session.close()

    return {"camera_id": camera_id, "window": window, "data": rows}


@router.get("/{camera_id}/counts/recent")
async def get_counts_recent(
    camera_id: str,
    limit: int = Query(50, ge=1, le=200),
):
    """Return the most recent crossing events for a camera."""
    validate_identifier(camera_id, name="camera_id")

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)
        events = repo.get_lane_changes(camera_id, limit=limit)
    except Exception:
        logger.error("Failed to query recent counts for %s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable — check connection and schema")
    finally:
        session.close()

    return {"camera_id": camera_id, "events": events}
