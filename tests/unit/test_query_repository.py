from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tf_db.models import LaneChangeEvent, VehicleCountEvent
from tf_db.repositories import SqlQueryRepository
from tf_db.session import SessionLocal


def test_get_latest_occupancy_counts_distinct_tracks(isolated_runtime):
    session = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        session.add_all(
            [
                VehicleCountEvent(
                    camera_id="CAM_OCC",
                    job_id="job-occ",
                    lane_id="lane_a",
                    track_id=1,
                    vehicle_type="car",
                    direction="forward",
                    confidence=0.9,
                    frame_id=1,
                    created_at=now - timedelta(seconds=5),
                ),
                VehicleCountEvent(
                    camera_id="CAM_OCC",
                    job_id="job-occ",
                    lane_id="lane_a",
                    track_id=1,
                    vehicle_type="car",
                    direction="forward",
                    confidence=0.91,
                    frame_id=2,
                    created_at=now - timedelta(seconds=2),
                ),
                VehicleCountEvent(
                    camera_id="CAM_OCC",
                    job_id="job-occ",
                    lane_id="lane_a",
                    track_id=2,
                    vehicle_type="truck",
                    direction="forward",
                    confidence=0.87,
                    frame_id=3,
                    created_at=now - timedelta(seconds=1),
                ),
            ]
        )
        session.commit()

        repo = SqlQueryRepository(session)
        occupancy = repo.get_latest_occupancy("CAM_OCC")
    finally:
        session.close()

    assert occupancy == {"lane_a": 2}


def test_get_recent_events_returns_descending_timestamps(isolated_runtime):
    session = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        session.add_all(
            [
                VehicleCountEvent(
                    camera_id="CAM_RECENT",
                    job_id="job-recent",
                    lane_id="lane_b",
                    track_id=21,
                    vehicle_type="bus",
                    direction="forward",
                    confidence=0.8,
                    frame_id=20,
                    created_at=now - timedelta(seconds=20),
                ),
                VehicleCountEvent(
                    camera_id="CAM_RECENT",
                    job_id="job-recent",
                    lane_id="lane_b",
                    track_id=22,
                    vehicle_type="car",
                    direction="forward",
                    confidence=0.95,
                    frame_id=21,
                    created_at=now - timedelta(seconds=10),
                ),
                VehicleCountEvent(
                    camera_id="CAM_RECENT",
                    job_id="job-recent",
                    lane_id="lane_b",
                    track_id=23,
                    vehicle_type="motorcycle",
                    direction="backward",
                    confidence=0.78,
                    frame_id=22,
                    created_at=now,
                ),
            ]
        )
        session.commit()

        repo = SqlQueryRepository(session)
        events = repo.get_recent_events("CAM_RECENT", limit=3)
    finally:
        session.close()

    assert [event["track_id"] for event in events] == [23, 22, 21]


def test_get_lane_changes_reads_true_lane_change_table(isolated_runtime):
    session = SessionLocal()
    try:
        session.add(
            LaneChangeEvent(
                camera_id="CAM_LANE",
                track_id=9,
                class_name="car",
                previous_lane_id="lane_a",
                current_lane_id="lane_b",
                frame_id=42,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        events = SqlQueryRepository(session).get_lane_changes("CAM_LANE")
    finally:
        session.close()

    assert events[0]["previous_lane_id"] == "lane_a"
    assert events[0]["current_lane_id"] == "lane_b"
