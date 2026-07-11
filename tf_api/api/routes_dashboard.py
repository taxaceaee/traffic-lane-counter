"""Dashboard API — fleet overview with real DB + live pipeline fusion.

Provides:
- GET /api/dashboard/summary — totals, per-camera, types, lanes, live status
- GET /api/dashboard/hourly — 24h buckets + fixed morning/evening/off-peak

Live session tallies (unique tracks) come from in-memory stream_meta when
pipelines are running; DB crossings provide historical line-count totals.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException

from tf_api.api.routes_auth import get_current_user

logger = logging.getLogger("trafficflow.dashboard")

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _load_all_camera_ids() -> list[str]:
    cam_dir = Path("configs/cameras")
    if not cam_dir.exists():
        return []
    return sorted(p.stem for p in cam_dir.glob("*.yaml"))


def _count_configured_lanes() -> int:
    """Count lane polygons from configs/lanes/*_lanes.yaml (not event-dependent)."""
    lanes_dir = Path("configs/lanes")
    if not lanes_dir.exists():
        return 0
    total = 0
    for path in lanes_dir.glob("*_lanes.yaml"):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            lanes = raw.get("lanes") or []
            if isinstance(lanes, list):
                total += len(lanes)
        except Exception:
            logger.debug("Failed reading lanes file %s", path, exc_info=True)
    return total


def _sum_vehicle_types(types: dict[str, Any] | None) -> int:
    if not types:
        return 0
    total = 0
    for v in types.values():
        try:
            n = int(v)
            if n > 0:
                total += n
        except (TypeError, ValueError):
            continue
    return total


def _merge_type_counts(*dicts: dict[str, int]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in dicts:
        for k, v in (d or {}).items():
            key = str(k).lower().strip()
            if not key:
                continue
            try:
                out[key] = out.get(key, 0) + int(v)
            except (TypeError, ValueError):
                continue
    return out


def _collect_live_snapshot() -> dict[str, Any]:
    """Read live pipeline memory for all active streams."""
    from tf_common.monitoring.live_metrics import get_camera_metrics

    try:
        from tf_api.api.routes_live import _lock, _streams
    except Exception:
        return {"cameras": {}, "live_cameras": 0, "types": {}, "total": 0}

    cameras: dict[str, dict[str, Any]] = {}
    types_live: dict[str, int] = {}
    live_cameras = 0

    with _lock:
        items = list(_streams.items())

    for cid, (_core, _q, meta, _sw, _sess, _stop) in items:
        metrics = get_camera_metrics(cid)
        vtypes = dict(meta.get("vehicle_types") or {})
        occ = dict(meta.get("occupancy") or {})
        connections = int(meta.get("connections", 0) or 0)
        stream_active = bool(metrics.get("stream_active")) or connections > 0
        if stream_active:
            live_cameras += 1
        live_total = _sum_vehicle_types(vtypes)
        types_live = _merge_type_counts(types_live, vtypes)
        cameras[cid] = {
            "camera_id": cid,
            "live": stream_active,
            "connections": connections,
            "total_live": live_total,
            "vehicle_types": vtypes,
            "occupancy": occ,
            "process_fps": float(metrics.get("process_fps") or 0.0),
            "output_fps": float(metrics.get("output_fps") or 0.0),
            "status": metrics.get("status"),
        }

    return {
        "cameras": cameras,
        "live_cameras": live_cameras,
        "types": types_live,
        "total": _sum_vehicle_types(types_live),
    }


def _build_hourly_buckets(
    repo: Any,
    camera_id: str | None,
    start: datetime,
    end: datetime,
) -> tuple[dict[int, int], str]:
    hourly = {h: 0 for h in range(24)}
    rows = repo.get_counts_timeseries(
        camera_id=camera_id or None,
        since=start,
        until=end,
        window="1hour",
        limit=168,
    )
    for r in rows:
        ts = r.get("timestamp", "")
        try:
            hour = datetime.fromisoformat(str(ts)).hour
            hourly[hour] = hourly.get(hour, 0) + int(r.get("count") or 0)
        except Exception:
            logger.debug("Skipping malformed dashboard hourly bucket", exc_info=True)

    if sum(hourly.values()) == 0:
        for r in repo.get_counts_hourly_from_events(
            camera_id=camera_id or None,
            since=start,
            until=end,
        ):
            h = int(r.get("hour", -1))
            if 0 <= h <= 23:
                hourly[h] = hourly.get(h, 0) + int(r.get("count") or 0)
        return hourly, "events"
    return hourly, "aggregates"


def _fixed_window_peaks(hourly: dict[int, int]) -> tuple[list[dict[str, Any]], int]:
    """Morning 7–10, evening 16–20, off-peak 22–5 (inclusive hour indices)."""
    morning_hours = list(range(7, 11))
    evening_hours = list(range(16, 21))
    offpeak_hours = list(range(0, 6)) + [22, 23]

    def _sum(hours: list[int]) -> int:
        return int(sum(hourly.get(h, 0) for h in hours))

    def _avg(hours: list[int]) -> int:
        return int(_sum(hours) // max(len(hours), 1))

    peaks = [
        {"label": "morning_peak", "hours": "07–10", "count": _sum(morning_hours), "avg": _avg(morning_hours)},
        {"label": "evening_peak", "hours": "16–20", "count": _sum(evening_hours), "avg": _avg(evening_hours)},
        {"label": "offpeak", "hours": "22–05", "count": _sum(offpeak_hours), "avg": _avg(offpeak_hours)},
    ]
    return peaks, _avg(offpeak_hours)


@router.get("/summary")
async def dashboard_summary(_user: dict = Depends(get_current_user)):
    """Fleet summary: DB crossings (24h) + live session tallies + configured lanes."""
    from tf_db.repositories import SqlQueryRepository
    from tf_db.session import SessionLocal

    camera_ids = _load_all_camera_ids()
    live = _collect_live_snapshot()
    live_by_cam: dict[str, dict[str, Any]] = live["cameras"]

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)

        total_vehicles_db = 0
        type_dist_db: dict[str, int] = {}
        per_camera: list[dict[str, Any]] = []

        for cid in camera_ids:
            cam_total = int(repo.get_counts_total(camera_id=cid) or 0)
            total_vehicles_db += cam_total

            rows = repo.get_counts_summary(camera_id=cid)
            for r in rows:
                vt = str(r.get("vehicle_type") or "unknown").lower()
                type_dist_db[vt] = type_dist_db.get(vt, 0) + int(r.get("count") or 0)

            live_info = live_by_cam.get(cid) or {}
            total_live = int(live_info.get("total_live") or 0)
            is_live = bool(live_info.get("live"))
            display_total = total_live if is_live and total_live > 0 else cam_total

            per_camera.append({
                "camera_id": cid,
                "total": display_total,
                "total_db": cam_total,
                "total_live": total_live,
                "live": is_live,
                "process_fps": live_info.get("process_fps", 0.0),
                "output_fps": live_info.get("output_fps", 0.0),
                "occupancy": live_info.get("occupancy") or {},
                "vehicle_types": live_info.get("vehicle_types") or {},
            })

        # Include any live-only camera ids not in YAML (shouldn't happen, but safe).
        for cid, live_info in live_by_cam.items():
            if cid in camera_ids:
                continue
            total_live = int(live_info.get("total_live") or 0)
            per_camera.append({
                "camera_id": cid,
                "total": total_live,
                "total_db": 0,
                "total_live": total_live,
                "live": bool(live_info.get("live")),
                "process_fps": live_info.get("process_fps", 0.0),
                "output_fps": live_info.get("output_fps", 0.0),
                "occupancy": live_info.get("occupancy") or {},
                "vehicle_types": live_info.get("vehicle_types") or {},
            })

        per_camera.sort(
            key=lambda x: (int(x.get("live") or 0), int(x.get("total") or 0)),
            reverse=True,
        )

        total_live = int(live.get("total") or 0)
        live_cameras = int(live.get("live_cameras") or 0)
        type_live = dict(live.get("types") or {})

        # Prefer live session when any stream is active and has tracks.
        if live_cameras > 0 and total_live > 0:
            total_vehicles = total_live
            type_distribution = type_live if type_live else type_dist_db
            data_source = "live_session"
        else:
            total_vehicles = total_vehicles_db
            type_distribution = type_dist_db if type_dist_db else type_live
            data_source = "db_24h"

        active_alerts = 0
        try:
            from tf_common.alert_service import alert_service
            active_alerts = alert_service.get_active_count()
        except Exception:
            logger.debug("Could not read active alert count", exc_info=True)

        return {
            "total_vehicles": total_vehicles,
            "total_vehicles_db": total_vehicles_db,
            "total_vehicles_live": total_live,
            "data_source": data_source,
            "per_camera": per_camera,
            "type_distribution": type_distribution,
            "type_distribution_db": type_dist_db,
            "type_distribution_live": type_live,
            "active_alerts": active_alerts,
            "total_cameras": len(camera_ids),
            "live_cameras": live_cameras,
            "total_lanes": _count_configured_lanes(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        logger.error("Dashboard summary query failed", exc_info=True)
        raise HTTPException(503, "Dashboard data unavailable — check database readiness") from None
    finally:
        session.close()


@router.get("/hourly")
async def dashboard_hourly(
    camera_id: str | None = None,
    date: str | None = None,
    _user: dict = Depends(get_current_user),
):
    """Hourly traffic breakdown with fixed-window peak labels."""
    from tf_db.repositories import SqlQueryRepository
    from tf_db.session import SessionLocal

    target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day = date_cls.fromisoformat(target_date)
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    session = SessionLocal()
    try:
        repo = SqlQueryRepository(session)
        hourly, source = _build_hourly_buckets(repo, camera_id, start, end)
    except Exception:
        logger.error("Hourly query failed", exc_info=True)
        raise HTTPException(503, "Dashboard hourly data unavailable — check database readiness") from None
    finally:
        session.close()

    hourly_list = [{"hour": h, "count": int(hourly.get(h, 0))} for h in range(24)]
    total = sum(int(hourly.get(h, 0)) for h in range(24))
    peaks, offpeak_avg = _fixed_window_peaks({h: int(hourly.get(h, 0)) for h in range(24)})

    return {
        "date": target_date,
        "total": total,
        "hourly": hourly_list,
        "peak_hours": peaks,
        "offpeak_avg": offpeak_avg,
        "source": source,
    }
