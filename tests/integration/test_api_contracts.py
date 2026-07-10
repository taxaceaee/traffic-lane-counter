from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tf_db.models import TrafficAggregate, VehicleCountEvent
from tf_db.session import SessionLocal


def test_counts_recent_requires_auth(client):
    response = client.get("/api/cameras/CAM_TEST/counts/recent")
    assert response.status_code == 401


def test_dashboard_requires_auth(client):
    response = client.get("/api/dashboard/summary")
    assert response.status_code == 401


def test_counts_recent_returns_raw_events(client, auth_headers):
    session = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        session.add_all(
            [
                VehicleCountEvent(
                    camera_id="CAM_TEST",
                    job_id="job-1",
                    lane_id="lane_a",
                    track_id=10,
                    vehicle_type="car",
                    direction="forward",
                    confidence=0.92,
                    frame_id=101,
                    created_at=now - timedelta(seconds=5),
                    crop_path="crop-a.jpg",
                ),
                VehicleCountEvent(
                    camera_id="CAM_TEST",
                    job_id="job-1",
                    lane_id="lane_a",
                    track_id=11,
                    vehicle_type="truck",
                    direction="backward",
                    confidence=0.88,
                    frame_id=102,
                    created_at=now,
                    crop_path="crop-b.jpg",
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    response = client.get(
        "/api/cameras/CAM_TEST/counts/recent?limit=10",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["camera_id"] == "CAM_TEST"
    assert [event["track_id"] for event in payload["events"]] == [11, 10]
    assert payload["events"][0]["crop_path"] == "crop-b.jpg"


def test_settings_put_requires_admin(client, auth_headers):
    response = client.put(
        "/api/settings",
        json={"system": {"max_streams": 12}},
        headers=auth_headers("viewer"),
    )
    assert response.status_code == 403


def test_settings_roundtrip_uses_frontend_schema(client, auth_headers):
    update_payload = {
        "api_url": "http://localhost:8018",
        "detection": {"confidence": 0.42, "detect_every_n_frames": 3},
        "system": {"max_workers": 6, "max_streams": 12},
        "appearance": {"refresh_interval_s": 45, "timezone": "Asia/Ho_Chi_Minh"},
    }

    put_response = client.put(
        "/api/settings",
        json=update_payload,
        headers=auth_headers(),
    )
    assert put_response.status_code == 200, put_response.text

    get_response = client.get("/api/settings", headers=auth_headers())
    assert get_response.status_code == 200
    settings = get_response.json()
    assert settings["api_url"] == "http://localhost:8018"
    assert settings["detection"]["confidence"] == 0.42
    assert settings["detection"]["detect_every_n_frames"] == 3
    assert settings["system"]["max_workers"] == 6
    assert settings["system"]["max_streams"] == 12
    assert settings["appearance"]["refresh_interval_s"] == 45
    assert settings["appearance"]["timezone"] == "Asia/Ho_Chi_Minh"


def test_settings_reset_uses_backend_defaults(client, auth_headers):
    put_response = client.put(
        "/api/settings",
        json={
            "api_url": "http://localhost:9999",
            "system": {"max_streams": 12},
            "appearance": {"refresh_interval_s": 45},
        },
        headers=auth_headers(),
    )
    assert put_response.status_code == 200, put_response.text

    reset_response = client.post("/api/settings/reset", headers=auth_headers())
    assert reset_response.status_code == 200, reset_response.text
    payload = reset_response.json()["settings"]
    assert payload["api_url"] == "http://localhost:8000"
    assert payload["system"]["max_streams"] == 16
    assert payload["appearance"]["refresh_interval_s"] == 30


def test_reports_require_auth(client):
    response = client.get("/api/reports/CAM_TEST/lanes")
    assert response.status_code == 401


def test_zones_require_operator_for_write(client, auth_headers):
    response = client.put(
        "/api/cameras/CAM_TEST/zones",
        json={
            "zones": [
                {
                    "zone_id": "zone_a",
                    "name": "Zone A",
                    "polygon": [[10, 10], [100, 10], [100, 100]],
                }
            ]
        },
        headers=auth_headers("viewer"),
    )
    assert response.status_code == 403


def test_camera_list_uses_only_approved_youtube_sources(client, auth_headers):
    response = client.get("/api/cameras", headers=auth_headers())
    assert response.status_code == 200

    cameras = response.json()
    sources = {camera["source"] for camera in cameras}
    camera_ids = {camera["camera_id"] for camera in cameras}

    assert camera_ids == {"CAM_TEST", "YT_LIVE_TEST", "YT_LIVE_TEST_02", "YT_LIVE_TEST_03"}
    assert {
        "https://www.youtube.com/watch?v=sJvEFrG0wq0",
        "https://www.youtube.com/watch?v=1EamsYw_Xyo",
        "https://www.youtube.com/watch?v=G_G8A6JU_LI",
    }.issubset(sources)


def test_dashboard_hourly_aggregates_real_hour_buckets(client, auth_headers):
    session = SessionLocal()
    ts = datetime(2026, 7, 9, 8, 0, tzinfo=timezone.utc)
    try:
        session.add_all(
            [
                TrafficAggregate(
                    camera_id="CAM_TEST",
                    lane_id="lane_a",
                    vehicle_type="car",
                    window="1hour",
                    window_start=ts,
                    count=7,
                    updated_at=ts,
                ),
                TrafficAggregate(
                    camera_id="CAM_TEST",
                    lane_id="lane_b",
                    vehicle_type="truck",
                    window="1hour",
                    window_start=ts,
                    count=3,
                    updated_at=ts,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    response = client.get(
        "/api/dashboard/hourly?camera_id=CAM_TEST&date=2026-07-09",
        headers=auth_headers("admin"),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["date"] == "2026-07-09"
    assert payload["total"] == 10
    assert payload["hourly"][8]["count"] == 10
