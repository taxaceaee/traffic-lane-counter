#!/usr/bin/env python3
"""Seed the database with sample crossing events for development/testing.

Usage::

    python -m scripts.seed_db                             # insert sample events
    python -m scripts.seed_db --camera CAM_STREAM_TEST     # specific camera
    python -m scripts.seed_db --count 1000                 # number of events
    python -m scripts.seed_db --clear                      # clear existing data first

This creates synthetic events so the dashboard shows data even when no
inference job has been run.  For production, use ``import_jsonl.py``
with real pipeline output.
"""

from __future__ import annotations

import argparse
import logging
import random
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("seed_db")

VEHICLE_TYPES = ["car", "motorcycle", "truck", "bus"]
DIRECTIONS = ["forward", "backward"]
LANES = ["lane_1", "lane_2"]


def seed_database(
    camera_id: str = "CAM_STREAM_TEST",
    count: int = 200,
    clear: bool = False,
    days_back: int = 1,
) -> None:
    """Insert synthetic crossing events into the database."""
    from tf_api.storage_adapters import make_server_adapters
    from tf_db.session import SessionLocal

    session = SessionLocal()
    adapter = make_server_adapters(session)

    try:
        if clear and adapter.events is not None:
            from tf_db.models import VehicleCountEvent

            deleted = session.query(VehicleCountEvent).filter(
                VehicleCountEvent.camera_id == camera_id,
            ).delete()
            session.commit()
            logger.info("Cleared %d existing events for %s", deleted, camera_id)

        now = datetime.now(timezone.utc)
        rng = random.SystemRandom()
        inserted = 0
        for i in range(count):
            ts = now - timedelta(
                seconds=rng.randint(0, days_back * 86400),
            )
            event = {
                "camera_id": camera_id,
                "job_id": "seed",
                "lane_id": rng.choice(LANES),
                "track_id": rng.randint(1, 500),
                "vehicle_type": rng.choice(VEHICLE_TYPES),
                "direction": rng.choice(DIRECTIONS),
                "confidence": round(rng.uniform(0.5, 0.99), 2),
                "frame_id": i + 1,
                "timestamp": ts,
            }
            if adapter.events is not None:
                adapter.events.insert_event(event)
                inserted += 1

        logger.info("Inserted %d sample events for camera %s", inserted, camera_id)
        adapter.commit()

    finally:
        adapter.close()
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed database with synthetic crossing events for dev/testing.",
    )
    parser.add_argument("--camera", default="CAM_STREAM_TEST", help="Camera ID")
    parser.add_argument("--count", type=int, default=200, help="Number of events")
    parser.add_argument("--clear", action="store_true", help="Clear existing data first")
    parser.add_argument("--days", type=int, default=1, help="Spread events over N days")
    args = parser.parse_args()

    seed_database(
        camera_id=args.camera,
        count=args.count,
        clear=args.clear,
        days_back=args.days,
    )


if __name__ == "__main__":
    main()
