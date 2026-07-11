"""Jobs API — launch and monitor inference jobs.

24/7 operational hardening:
- Max concurrent jobs: limits resource exhaustion
- In-memory job registry with bounded retention
- Cleanup of old completed/failed jobs after 7 days
"""

import csv
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from tf_api.api.routes_auth import get_current_user, require_operator
from tf_api.services.settings_service import get_max_workers
from tf_common.safe_path import validate_identifier
from tf_core.config.compiler import ConfigError, compile_camera_config, write_compiled_config
from tf_core.config.loader import resolve_path
from tf_worker.pipeline import TrafficFlowPipeline

logger = logging.getLogger("trafficflow.jobs")

router = APIRouter(prefix="/api", tags=["jobs"])

_OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "data/outputs"))
_CONFIGS_DIR = Path("configs")
_JOB_RETENTION_DAYS = 7

_JOBS: dict[str, dict[str, Any]] = {}
_active_jobs: set[str] = set()
_jobs_lock = threading.Lock()


def _persist_job(job: dict[str, Any]) -> None:
    from tf_db.repositories import SqlJobRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        SqlJobRepository(session).create({
            "id": job["job_id"],
            "camera_id": job["camera_id"],
            "model_id": job["model_id"],
            "status": job["status"],
            "progress": job.get("progress", 0),
            "output_dir": job["output_dir"],
            "source": job.get("source", ""),
            "created_at": datetime.fromisoformat(job["created_at"]),
            "started_at": datetime.now(timezone.utc),
        })
    finally:
        session.close()


def _update_persisted_job(job_id: str, updates: dict[str, Any]) -> None:
    from tf_db.repositories import SqlJobRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        SqlJobRepository(session).update(job_id, updates)
    finally:
        session.close()


class InferVideoRequest(BaseModel):
    camera_id: str
    model_id: str | None = None
    save_annotated: bool = True


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    camera_id: str
    model_id: str


def _get_camera_config_path(camera_id: str) -> Path:
    validate_identifier(camera_id, name="camera_id")
    return _CONFIGS_DIR / "cameras" / f"{camera_id}.yaml"


def _cleanup_old_jobs():
    """Remove jobs older than retention period to prevent memory leak."""
    now = datetime.now(timezone.utc)
    with _jobs_lock:
        dead = [
            jid for jid, job in list(_JOBS.items())
            if job.get("status") in ("completed", "failed")
            and "created_at" in job
            and (now - datetime.fromisoformat(job["created_at"])).days >= _JOB_RETENTION_DAYS
        ]
        for jid in dead:
            _JOBS.pop(jid, None)
        if dead:
            logger.info("Cleaned up %d old jobs", len(dead))


@router.post("/infer/video", status_code=202)
async def submit_video(
    req: InferVideoRequest,
    response: Response,
    _user: dict = Depends(require_operator),
):
    """Submit a video inference job. Returns 202 with job_id."""
    validate_identifier(req.camera_id, name="camera_id")
    cam_path = _get_camera_config_path(req.camera_id)
    if not cam_path.exists():
        raise HTTPException(404, f"Camera config not found: {req.camera_id}")

    _cleanup_old_jobs()
    max_jobs = max(1, min(get_max_workers(), 16))

    with _jobs_lock:
        if len(_active_jobs) >= max_jobs:
            raise HTTPException(429, f"Max {max_jobs} concurrent jobs — try later")
        job_id = str(uuid.uuid4())
        _active_jobs.add(job_id)

    try:
        compiled = compile_camera_config(
            camera_config_path=cam_path,
            job_id=job_id,
        )
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        with _jobs_lock:
            _active_jobs.discard(job_id)
        raise HTTPException(422, detail=str(exc)) from exc

    compiled.setdefault("server", {})
    compiled["server"]["job_id"] = job_id
    if req.model_id:
        compiled["server"]["model_id"] = req.model_id
        # Override detector weights from models registry so the job actually
        # uses the selected model instead of the camera's default.
        models_config_path = _CONFIGS_DIR / "models.yaml"
        if models_config_path.exists():
            with open(models_config_path, encoding="utf-8") as _fm:  # noqa: ASYNC230 - tiny local registry
                _models_raw = yaml.safe_load(_fm) or {}
            model_found = False
            for _m in _models_raw.get("models", []):
                if _m.get("model_id") == req.model_id:
                    model_found = True
                    compiled["detector"]["weights"] = str(
                        resolve_path(_m["path"])
                    )
                    compiled["detector"]["class_mode"] = _m.get("class_mode", "coco_pretrained")
                    break
            if not model_found:
                with _jobs_lock:
                    _active_jobs.discard(job_id)
                raise HTTPException(404, f"Model not found: {req.model_id}")

    job_dir = _OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_config = job_dir / "config_compiled.yaml"
    write_compiled_config(output_config, compiled)

    source = compiled.get("server", {}).get("source", "")
    job_record = {
            "job_id": job_id,
            "camera_id": req.camera_id,
            "model_id": compiled.get("server", {}).get("model_id", ""),
            "status": "running",
            "progress": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config_path": str(output_config),
            "output_dir": str(job_dir),
            "source": source,
        }
    try:
        _persist_job(job_record)
    except Exception as exc:
        with _jobs_lock:
            _active_jobs.discard(job_id)
        logger.exception("Could not persist job %s", job_id)
        raise HTTPException(503, "Database unavailable while creating job") from exc
    with _jobs_lock:
        _JOBS[job_id] = job_record

    def _run():
        pipeline = None
        try:
            pipeline = TrafficFlowPipeline(
                config_path=output_config,
                output_dir=job_dir,
                source=source,
            )
            result = pipeline.run()
            # Persist the output before reporting success.  A completed pipeline
            # with a failed ingest is not a completed business job.
            ingest_count = _auto_ingest(job_dir, req.camera_id, job_id)
            with _jobs_lock:
                _JOBS[job_id].update({
                    "status": "completed",
                    "progress": 100,
                    "total_frames": result.get("total_frames", 0),
                    "fps": _avg_fps(result.get("timing_records", {})),
                    "ingested_events": ingest_count,
                })
            _update_persisted_job(job_id, {
                "status": "completed",
                "progress": 100,
                "total_frames": result.get("total_frames", 0),
                "fps": _avg_fps(result.get("timing_records", {})),
                "ingested_events": ingest_count,
                "finished_at": datetime.now(timezone.utc),
            })

        except Exception:
            logger.exception("Job %s failed", job_id)
            with _jobs_lock:
                _JOBS[job_id].update({"status": "failed", "error": "inference or ingest failed"})
            try:
                _update_persisted_job(job_id, {
                    "status": "failed",
                    "error": "inference or ingest failed",
                    "finished_at": datetime.now(timezone.utc),
                })
            except Exception:
                logger.exception("Could not persist failed status for job %s", job_id)
        finally:
            if pipeline is not None:
                try:
                    pipeline.stop()
                except Exception:
                    logger.warning("Pipeline cleanup failed for job %s", job_id, exc_info=True)
            with _jobs_lock:
                _active_jobs.discard(job_id)

    def _auto_ingest(output_dir: Path, camera_id: str, jid: str) -> int:
        """Import JSONL output atomically and return the inserted event count."""
        session = None
        try:
            from tf_api.storage_adapters import make_server_adapters
            from tf_db.session import SessionLocal

            jsonl_files = sorted(output_dir.glob("*.jsonl"))
            lane_changes_path = output_dir / "lane_changes.csv"
            if not jsonl_files and not lane_changes_path.exists():
                logger.info("No JSONL files to ingest for job %s", jid)
                return 0

            session = SessionLocal()
            adapter = make_server_adapters(session)
            total = 0

            def _buckets(ts: datetime) -> list[dict[str, Any]]:
                starts = {
                    "1min": ts.replace(second=0, microsecond=0),
                    "5min": ts.replace(
                        minute=(ts.minute // 5) * 5, second=0, microsecond=0
                    ),
                    "1hour": ts.replace(minute=0, second=0, microsecond=0),
                    "1day": ts.replace(hour=0, minute=0, second=0, microsecond=0),
                }
                return [
                    {"window": window, "window_start": start, "updated_at": ts}
                    for window, start in starts.items()
                ]

            def _insert(event: dict[str, Any]) -> None:
                nonlocal total
                adapter.events.insert_event(event)
                adapter.aggregates.upsert_buckets(
                    camera_id=event["camera_id"],
                    lane_id=event["lane_id"],
                    vehicle_type=event["vehicle_type"],
                    buckets=_buckets(event["timestamp"]),
                )
                total += 1

            for jsonl_path in jsonl_files:
                with open(jsonl_path, encoding="utf-8") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Parse crossing events
                        crossings = data.get("crossings", [])
                        if crossings:
                            ts = data.get("frame_timestamp") or data.get("timestamp")
                            if isinstance(ts, str):
                                try:
                                    from datetime import timezone
                                    ts = datetime.fromisoformat(ts)
                                except (ValueError, TypeError):
                                    ts = datetime.now(timezone.utc)
                            if ts is None:
                                ts = datetime.now(timezone.utc)
                            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)

                            for cx in crossings:
                                direction = cx.get("direction", "")
                                if isinstance(direction, list):
                                    direction = direction[0] if direction else ""
                                ev = {
                                    "camera_id": camera_id,
                                    "job_id": jid,
                                    "lane_id": cx.get("lane_id", "unknown"),
                                    "track_id": int(cx.get("track_id", -1)),
                                    "vehicle_type": cx.get("class_name", "unknown"),
                                    "direction": direction,
                                    "confidence": float(cx.get("confidence", 0.0)),
                                    "frame_id": int(data.get("frame", 0)),
                                    "timestamp": ts,
                                }
                                _insert(ev)

                        # Parse flat event format
                        elif "lane_id" in data and "track_id" in data:
                            ts = data.get("timestamp") or data.get("frame_timestamp") or datetime.now(timezone.utc)
                            if isinstance(ts, str):
                                try:
                                    ts = datetime.fromisoformat(ts)
                                except (ValueError, TypeError):
                                    ts = datetime.now(timezone.utc)
                            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            direction = data.get("direction", "")
                            if isinstance(direction, list):
                                direction = direction[0] if direction else ""
                            ev = {
                                "camera_id": camera_id,
                                "job_id": jid,
                                "lane_id": data.get("lane_id", "unknown"),
                                "track_id": int(data.get("track_id", -1)),
                                "vehicle_type": data.get("vehicle_type", data.get("class_name", "unknown")),
                                "direction": direction,
                                "confidence": float(data.get("confidence", 0.0)),
                                "frame_id": int(data.get("frame_id", data.get("frame", 0))),
                                "timestamp": ts,
                            }
                            _insert(ev)

            lane_change_repo = getattr(adapter, "lane_changes", None)
            if lane_change_repo is not None and lane_changes_path.exists():
                with open(lane_changes_path, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        try:
                            lane_change_repo.insert_event({
                                "camera_id": camera_id,
                                "track_id": int(row["track_id"]),
                                "class_name": row.get("class_name", "unknown"),
                                "previous_lane_id": row.get("previous_stable_lane"),
                                "current_lane_id": row["current_stable_lane"],
                                "frame_id": int(row["frame"]),
                                "created_at": datetime.now(timezone.utc),
                            })
                        except (KeyError, TypeError, ValueError):
                            logger.warning("Skipping malformed lane-change row in %s", lane_changes_path)

            adapter.events.commit()
            adapter.aggregates.commit()
            if total:
                logger.info("Auto-ingested %d events from job %s into DB", total, jid)
            return total
        except Exception:
            if session is not None:
                session.rollback()
            logger.warning("Auto-ingest failed for job %s", jid, exc_info=True)
            raise
        finally:
            if session is not None:
                session.close()

    def _avg_fps(tr: dict) -> float:
        tot = tr.get("detect_track", [])
        return round(len(tot) / (sum(tot) / 1000), 1) if tot else 0.0

    threading.Thread(target=_run, daemon=True, name=f"job-{job_id[:8]}").start()

    response.headers["Location"] = f"/api/jobs/{job_id}"
    return JobCreateResponse(
        job_id=job_id,
        status="running",
        camera_id=req.camera_id,
        model_id=compiled.get("server", {}).get("model_id", ""),
    )


def get_job_stats() -> dict[str, int]:
    """Return aggregate job statistics used by the system health dashboard."""
    # The in-memory registry is useful for active thread bookkeeping, but the
    # DB is the durable source of truth after an API restart.
    try:
        from sqlalchemy import func

        from tf_db.models import InferenceJob
        from tf_db.session import SessionLocal

        session = SessionLocal()
        try:
            total = int(session.query(func.count(InferenceJob.id)).scalar() or 0)
            active = int(
                session.query(func.count(InferenceJob.id))
                .filter(InferenceJob.status.in_(("running", "queued")))
                .scalar()
                or 0
            )
            completed = int(
                session.query(func.count(InferenceJob.id))
                .filter(InferenceJob.status == "completed")
                .scalar()
                or 0
            )
            failed = int(
                session.query(func.count(InferenceJob.id))
                .filter(InferenceJob.status == "failed")
                .scalar()
                or 0
            )
            return {"total": total, "active": active, "completed": completed, "failed": failed}
        finally:
            session.close()
    except Exception:
        logger.debug("Falling back to in-memory job stats", exc_info=True)
    with _jobs_lock:
        total = len(_JOBS)
        active = len(_active_jobs)
        completed = sum(1 for j in _JOBS.values() if j.get("status") == "completed")
        failed = sum(1 for j in _JOBS.values() if j.get("status") == "failed")
    return {"total": total, "active": active, "completed": completed, "failed": failed}


@router.get("/jobs")
async def list_jobs(_user: dict = Depends(get_current_user)):
    from tf_db.repositories import SqlJobRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        return SqlJobRepository(session).list()
    finally:
        session.close()


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, _user: dict = Depends(get_current_user)):
    validate_identifier(job_id, name="job_id")
    from tf_db.repositories import SqlJobRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        job = SqlJobRepository(session).get(job_id)
    finally:
        session.close()
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    return job


@router.get("/jobs/{job_id}/video")
async def get_job_video(job_id: str, _user: dict = Depends(get_current_user)):
    validate_identifier(job_id, name="job_id")
    from tf_db.repositories import SqlJobRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        job = SqlJobRepository(session).get(job_id)
    finally:
        session.close()
    if not job:
        raise HTTPException(404, f"Job not found: {job_id}")
    video_path = Path(job.get("output_dir", "")) / "annotated.mp4"
    if not video_path.exists():
        raise HTTPException(404, "Video not found for this job")
    from fastapi.responses import FileResponse
    return FileResponse(str(video_path), media_type="video/mp4")
