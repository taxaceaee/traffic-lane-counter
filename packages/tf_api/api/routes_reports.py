"""Reports API — per-lane statistics and CSV export for each camera.

Provides:
- GET /api/reports/{camera_id}/lanes       — per-lane report (counts, types, direction, occupancy)
- GET /api/reports/{camera_id}/lanes/csv   — CSV download of the same data

All data comes from real database queries + lane YAML configs.
Falls back gracefully when DB has no data.
"""

import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from tf_api.api.routes_auth import get_current_user
from tf_common.safe_path import validate_identifier
from tf_db.repositories import SqlQueryRepository
from tf_db.session import SessionLocal

logger = logging.getLogger("trafficflow.reports")

router = APIRouter(prefix="/api/reports", tags=["reports"])

_LANES_DIR = Path("configs/lanes")


def _parse_dt(value: str | None) -> datetime | None:
    """Parse ISO timestamps from the SPA (``toISOString()`` uses trailing ``Z``)."""
    if not value:
        return None
    # Python 3.10 fromisoformat rejects trailing Z; normalize to +00:00.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_lane_names(camera_id: str) -> dict[str, str]:
    """Load lane_id → human-readable name mapping from lane YAML config."""
    lanes_path = _LANES_DIR / f"{camera_id}_lanes.yaml"
    if not lanes_path.exists():
        return {}
    try:
        with open(lanes_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return {
            item["lane_id"]: item.get("name", item["lane_id"])
            for item in raw.get("lanes", [])
            if "lane_id" in item
        }
    except Exception:
        logger.warning("Failed to load lane names for %s", camera_id, exc_info=True)
        return {}


def _build_lane_report_rows(
    camera_id: str,
    since_dt: datetime | None,
    until_dt: datetime | None,
) -> dict[str, Any]:
    """Core query logic — single DB session, all queries in one transaction."""
    lane_names = _load_lane_names(camera_id)

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)

        counts_rows = repo.get_counts_summary(
            camera_id=camera_id, since=since_dt, until=until_dt,
        )
        total = repo.get_counts_total(
            camera_id=camera_id, since=since_dt, until=until_dt,
        )
        latest_occ = repo.get_latest_occupancy(camera_id)
        dir_rows = repo.get_direction_summary(
            camera_id=camera_id, since=since_dt, until=until_dt,
        )
    except Exception:
        logger.error("Report query failed for %s", camera_id, exc_info=True)
        raise HTTPException(503, detail="Database unavailable") from None
    finally:
        session.close()

    # Aggregate per-lane counts
    lanes_map: dict[str, dict[str, int]] = {}
    all_lane_ids: set[str] = set(lane_names.keys())
    for r in counts_rows:
        all_lane_ids.add(r["lane_id"])
        lane = r["lane_id"]
        if lane not in lanes_map:
            lanes_map[lane] = {}
        lanes_map[lane][r["vehicle_type"]] = (
            lanes_map[lane].get(r["vehicle_type"], 0) + r["count"]
        )

    # Direction breakdown — aggregated from SQL GROUP BY (O(N_lanes) not O(N_events))
    dir_map: dict[str, dict[str, int]] = {}
    for r in dir_rows:
        lane = r["lane_id"]
        if lane not in dir_map:
            dir_map[lane] = {"forward": 0, "backward": 0}
        direction = r["direction"]
        dir_map[lane][direction] = r["count"]

    lanes_out: list[dict[str, Any]] = []
    for lane_id in sorted(
        all_lane_ids,
        key=lambda x: lane_names.get(x, x),
    ):
        types = lanes_map.get(lane_id, {})
        dirs = dir_map.get(lane_id, {})

        lanes_out.append({
            "lane_id": lane_id,
            "name": lane_names.get(lane_id, lane_id),
            "total": sum(types.values()),
            "types": types,
            "direction": {
                "forward": dirs.get("forward", 0),
                "backward": dirs.get("backward", 0),
            },
            "occupancy": latest_occ.get(lane_id, 0),
        })

    return {
        "camera_id": camera_id,
        "total": total,
        "lanes": lanes_out,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/{camera_id}/lanes")
async def get_lane_report(
    camera_id: str,
    since: str | None = Query(None, description="ISO datetime start"),
    until: str | None = Query(None, description="ISO datetime end"),
    _user: dict = Depends(get_current_user),
):
    """Per-lane statistics report for a camera.

    Returns each lane with:
      - human-readable name (from YAML config)
      - total vehicle count
      - per-type breakdown (car, motorcycle, truck, bus)
      - direction split (forward / backward) — aggregated, time-filtered
      - latest occupancy count
    """
    validate_identifier(camera_id, name="camera_id")

    try:
        since_dt = _parse_dt(since)
        until_dt = _parse_dt(until)
    except ValueError as exc:
        raise HTTPException(400, detail=f"Invalid since/until datetime: {exc}") from None

    return _build_lane_report_rows(camera_id, since_dt, until_dt)


@router.get("/{camera_id}/lanes/csv")
async def export_lane_report_csv(
    camera_id: str,
    since: str | None = Query(None),
    until: str | None = Query(None),
    _user: dict = Depends(get_current_user),
):
    """Export per-lane statistics report as a downloadable CSV file."""
    validate_identifier(camera_id, name="camera_id")

    try:
        since_dt = _parse_dt(since)
        until_dt = _parse_dt(until)
    except ValueError as exc:
        raise HTTPException(400, detail=f"Invalid since/until datetime: {exc}") from None

    report = _build_lane_report_rows(camera_id, since_dt, until_dt)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "lane_id", "lane_name", "total",
        "cars", "motorcycles", "trucks", "buses",
        "forward", "backward", "occupancy",
    ])

    for lane in report["lanes"]:
        types = lane["types"]
        dirs = lane["direction"]
        writer.writerow([
            lane["lane_id"],
            lane["name"],
            lane["total"],
            types.get("car", 0),
            types.get("motorcycle", 0),
            types.get("truck", 0),
            types.get("bus", 0),
            dirs.get("forward", 0),
            dirs.get("backward", 0),
            lane["occupancy"],
        ])

    output.seek(0)
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{camera_id}_lane_report_{now_str}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
