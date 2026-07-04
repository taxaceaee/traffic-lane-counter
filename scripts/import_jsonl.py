#!/usr/bin/env python3
"""Import pipeline JSONL output into the database.

Usage::

    # Import a specific output directory (looks for events.jsonl and crossings.jsonl)
    python -m scripts.import_jsonl outputs/<job_id>/

    # Import with camera_id override
    python -m scripts.import_jsonl outputs/<job_id>/ --camera-id CAM_01

    # Dry run — count records without writing
    python -m scripts.import_jsonl outputs/<job_id>/ --dry-run

The tool reads every JSONL file under the given directory, parses crossing
events and lane-change events, and inserts them into the database via the
same RepositoryBundle adapter used by the live pipeline.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("import_jsonl")


def _find_jsonl_files(output_dir: Path) -> list[Path]:
    """Find all .jsonl files in output_dir (non-recursive)."""
    return sorted(output_dir.glob("*.jsonl"))


def _parse_event(
    line: dict[str, Any],
    camera_id: str | None,
) -> dict[str, Any] | None:
    """Parse a JSONL line into an event dict compatible with SqlEventRepository.

    Handles two formats:
      - ``detection`` / ``crossing`` lines from pipeline (have ``detections``
        or ``crossings`` arrays)
      - Flat ``vehicle_count_event`` style dicts (have ``vehicle_type`` + ``lane_id``)
    """
    # Flat event format
    if "lane_id" in line and ("track_id" in line or "vehicle_type" in line):
        ts = line.get("timestamp") or line.get("frame_timestamp") or datetime.now(timezone.utc)
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        direction = line.get("direction", "")
        if isinstance(direction, list):
            direction = direction[0] if direction else ""

        return {
            "camera_id": camera_id or line.get("camera_id", "imported"),
            "job_id": line.get("job_id", "import"),
            "lane_id": line.get("lane_id", "unknown"),
            "track_id": int(line.get("track_id", -1)),
            "vehicle_type": line.get("vehicle_type", line.get("class_name", "unknown")),
            "direction": direction,
            "confidence": float(line.get("confidence", 0.0)),
            "frame_id": int(line.get("frame_id", line.get("frame", 0))),
            "timestamp": ts,
        }

    # Crossing event format: {"frame": N, "crossings": [{"track_id": ..., ...}, ...]}
    crossings = line.get("crossings", [])
    if crossings:
        events = []
        ts = line.get("timestamp") or line.get("frame_timestamp") or datetime.now(timezone.utc)
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                ts = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        for cx in crossings:
            direction = cx.get("direction", "")
            if isinstance(direction, list):
                direction = direction[0] if direction else ""
            events.append({
                "camera_id": camera_id or cx.get("camera_id", "imported"),
                "job_id": cx.get("job_id", "import"),
                "lane_id": cx.get("lane_id", "unknown"),
                "track_id": int(cx.get("track_id", -1)),
                "vehicle_type": cx.get("class_name", "unknown"),
                "direction": direction,
                "confidence": float(cx.get("confidence", 0.0)),
                "frame_id": int(line.get("frame", 0)),
                "timestamp": ts,
            })
        return events

    # Detection record format: {"frame": N, "detections": [...], ...}
    # These don't have crossing info so skip
    return None


def import_file(
    path: Path,
    camera_id: str | None = None,
    dry_run: bool = False,
    session=None,
    adapter=None,
) -> dict[str, int]:
    """Import a single JSONL file.

    Returns {total, inserted, skipped, errors}.
    """
    stats: dict[str, int] = {"total": 0, "inserted": 0, "skipped": 0, "errors": 0}

    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            stats["total"] += 1

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("JSON decode error at %s:%d — %s", path.name, line_no, e)
                stats["errors"] += 1
                continue

            try:
                result = _parse_event(data, camera_id)
            except Exception as e:
                logger.warning("Parse error at %s:%d — %s", path.name, line_no, e)
                stats["errors"] += 1
                continue

            if result is None:
                stats["skipped"] += 1
                continue

            # result may be a single event or a list (from crossing format)
            events = result if isinstance(result, list) else [result]

            for ev in events:
                if ev is None:
                    continue
                if dry_run:
                    stats["inserted"] += 1
                    continue

                if adapter is not None:
                    try:
                        adapter.events.insert_event(ev)
                        stats["inserted"] += 1
                    except Exception as e:
                        logger.warning("Insert error: %s", e)
                        stats["errors"] += 1
                elif session is not None:
                    try:
                        from tf_db.repositories import SqlEventRepository

                        repo = SqlEventRepository(session)
                        repo.insert_event(ev)
                        stats["inserted"] += 1
                    except Exception as e:
                        logger.warning("Insert error: %s", e)
                        stats["errors"] += 1
                else:
                    stats["inserted"] += 1  # count-only mode

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import pipeline JSONL output into the database.",
    )
    parser.add_argument(
        "output_dir",
        type=str,
        help="Path to pipeline output directory (e.g. outputs/<job_id>/).",
    )
    parser.add_argument(
        "--camera-id",
        type=str,
        default=None,
        help="Override camera_id for imported events.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count records without writing to DB.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        logger.error("Output directory not found: %s", output_dir)
        sys.exit(1)

    jsonl_files = _find_jsonl_files(output_dir)
    if not jsonl_files:
        logger.warning("No JSONL files found in %s", output_dir)
        return

    # Connect to DB
    if not args.dry_run:
        from tf_db.session import SessionLocal
        from tf_api.storage_adapters import make_server_adapters

        session = SessionLocal()
        adapter = make_server_adapters(session)
        logger.info("Connected to database")
    else:
        session = None
        adapter = None

    total_stats: dict[str, int] = {"total": 0, "inserted": 0, "skipped": 0, "errors": 0}

    for jsonl_path in jsonl_files:
        logger.info("Importing %s …", jsonl_path.name)
        stats = import_file(
            jsonl_path,
            camera_id=args.camera_id,
            dry_run=args.dry_run,
            session=session,
            adapter=adapter,
        )
        for k, v in stats.items():
            total_stats[k] += v
        logger.info(
            "  → %s: %d total, %d inserted, %d skipped, %d errors",
            jsonl_path.name,
            stats["total"], stats["inserted"], stats["skipped"], stats["errors"],
        )

    if adapter is not None and not args.dry_run:
        adapter.close()
        session.close()

    logger.info(
        "Done: %d total, %d inserted, %d skipped, %d errors",
        total_stats["total"], total_stats["inserted"],
        total_stats["skipped"], total_stats["errors"],
    )


if __name__ == "__main__":
    main()
