"""Dashboard API — aggregated summary across all cameras.

Provides:
- GET /api/dashboard/summary — total vehicles, per-camera breakdown,
  vehicle type distribution, active alerts count.

All data comes from real database queries.  Falls back to empty/zero
when the DB has no data.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger("trafficflow.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _load_all_camera_ids():
    """Return all registered camera IDs from config files."""
    from pathlib import Path
    cam_dir = Path("configs/cameras")
    if not cam_dir.exists():
        return []
    return sorted(p.stem for p in cam_dir.glob("*.yaml"))


@router.get("/summary")
async def dashboard_summary():
    """Aggregated dashboard data: totals, per-camera, type distribution, alerts."""
    from tf_db.repositories import SqlQueryRepository
    from tf_db.session import SessionLocal

    camera_ids = _load_all_camera_ids()

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)

        total_vehicles = 0
        per_camera: list[dict[str, Any]] = []
        type_dist: dict[str, int] = {}
        total_lanes = 0

        for cid in camera_ids:
            cam_total = repo.get_counts_total(camera_id=cid)
            if cam_total:
                total_vehicles += cam_total

            rows = repo.get_counts_summary(camera_id=cid)
            if rows:
                total_lanes += len(set(r["lane_id"] for r in rows))
                for r in rows:
                    vt = r["vehicle_type"]
                    type_dist[vt] = type_dist.get(vt, 0) + r["count"]

            per_camera.append({
                "camera_id": cid,
                "total": cam_total,
            })

        per_camera.sort(key=lambda x: x["total"], reverse=True)

        active_alerts = 0
        try:
            from tf_common.alert_service import alert_service
            active_alerts = alert_service.get_active_count()
        except Exception:
            pass

        return {
            "total_vehicles": total_vehicles,
            "per_camera": per_camera,
            "type_distribution": type_dist,
            "active_alerts": active_alerts,
            "total_cameras": len(camera_ids),
            "total_lanes": total_lanes,
        }
    except Exception:
        logger.error("Dashboard summary query failed", exc_info=True)
        return {
            "total_vehicles": 0,
            "per_camera": [],
            "type_distribution": {},
            "active_alerts": 0,
            "total_cameras": len(camera_ids),
            "total_lanes": 0,
        }
    finally:
        session.close()


@router.get("/hourly")
async def dashboard_hourly(
    camera_id: str | None = None,
    date: str | None = None,
):
    """Hourly traffic breakdown with peak hours.

    Returns 24-element hourly array plus computed peak hours
    based on real database aggregates — no synthetic data.
    """
    from tf_db.repositories import SqlQueryRepository
    from tf_db.session import SessionLocal

    target_date = date or datetime.utcnow().strftime("%Y-%m-%d")
    start = datetime.fromisoformat(target_date)
    end = start + timedelta(days=1)

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)
        rows = repo.get_counts_timeseries(
            camera_id=camera_id or None,
            since=start.isoformat(),
            until=end.isoformat(),
            window="1hour",
            limit=168,
        )
    except Exception:
        logger.error("Hourly query failed", exc_info=True)
        rows = []
    finally:
        session.close()

    hourly = {h: 0 for h in range(24)}
    for r in rows:
        ts = r.get("timestamp", "")
        try:
            hour = datetime.fromisoformat(str(ts)).hour
            hourly[hour] = hourly.get(hour, 0) + r.get("count", 0)
        except Exception:
            pass

    hourly_list = [{"hour": h, "count": hourly[h]} for h in range(24)]
    total = sum(hourly[h] for h in hourly)

    sorted_hours = sorted(hourly_list, key=lambda h: h["count"], reverse=True)
    peaks = sorted_hours[:2] if sorted_hours else []
    for p in peaks:
        h = p["hour"]
        if 7 <= h <= 10:
            p["label"] = "morning_peak"
        elif 16 <= h <= 20:
            p["label"] = "evening_peak"
        else:
            p["label"] = "peak"

    offpeak_hours = [hourly[h] for h in range(24) if h <= 5 or h >= 22]
    offpeak_avg = sum(offpeak_hours) // max(len(offpeak_hours), 1) if offpeak_hours else 0

    return {
        "date": target_date,
        "total": total,
        "hourly": hourly_list,
        "peak_hours": peaks,
        "offpeak_avg": offpeak_avg,
    }
