from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tf_api.api.routes_auth import _hash_password
from tf_api.api.routes_auth import router as auth_router
from tf_api.api.routes_cameras import router as cameras_router
from tf_api.api.routes_counts import router as counts_router
from tf_api.api.routes_dashboard import router as dashboard_router
from tf_api.api.routes_reports import router as reports_router
from tf_api.api.routes_settings import router as settings_router
from tf_api.api.routes_zones import router as zones_router
from tf_db.models import Base
from tf_db.repositories import SqlUserRepository
from tf_db.session import SessionLocal, get_engine


def _create_user(username: str, password: str, role: str) -> None:
    session = SessionLocal()
    try:
        repo = SqlUserRepository(session)
        repo.create(
            {
                "id": str(uuid.uuid4()),
                "username": username,
                "email": f"{username}@example.com",
                "password_hash": _hash_password(password),
                "role": role,
                "is_active": True,
            }
        )
    finally:
        session.close()


@pytest.fixture()
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    from tf_db import session as db_session_module

    # Clear engine + sessionmaker so DATABASE_URL switch takes effect.
    if hasattr(db_session_module, "reset_engine_cache"):
        db_session_module.reset_engine_cache()
    else:
        db_session_module.get_engine.cache_clear()
    engine = get_engine()
    Base.metadata.create_all(bind=engine)

    configs_dir = tmp_path / "configs"
    cameras_dir = configs_dir / "cameras"
    lanes_dir = configs_dir / "lanes"
    zones_dir = configs_dir / "detection_zones"
    settings_path = configs_dir / "settings.json"
    for directory in (cameras_dir, lanes_dir, zones_dir):
        directory.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("{}", encoding="utf-8")

    import tf_api.api.routes_cameras as routes_cameras
    import tf_api.api.routes_reports as routes_reports
    import tf_api.api.routes_settings as routes_settings
    import tf_api.api.routes_zones as routes_zones
    import tf_api.services.settings_service as settings_service

    monkeypatch.setattr(routes_cameras, "_CONFIGS_DIR", configs_dir)
    monkeypatch.setattr(routes_cameras, "_CAMERAS_DIR", cameras_dir)
    monkeypatch.setattr(routes_cameras, "_LANES_DIR", lanes_dir)
    monkeypatch.setattr(routes_cameras, "_ZONES_DIR", zones_dir)
    monkeypatch.setattr(routes_reports, "_LANES_DIR", lanes_dir)
    monkeypatch.setattr(routes_settings, "_SETTINGS_DIR", configs_dir)
    monkeypatch.setattr(routes_settings, "_SETTINGS_FILE", settings_path)
    monkeypatch.setattr(routes_zones, "_CONFIGS_DIR", configs_dir)
    monkeypatch.setattr(routes_zones, "_ZONES_DIR", zones_dir)
    monkeypatch.setattr(settings_service, "_SETTINGS_PATH", settings_path)

    (cameras_dir / "CAM_TEST.yaml").write_text(
        "\n".join(
            [
                "camera:",
                "  camera_id: CAM_TEST",
                "  name: Camera Test",
                "  source_type: youtube_live",
                "  source: https://www.youtube.com/watch?v=sJvEFrG0wq0",
                "  fps: 30.0",
                "  frame_size:",
                "    width: 1280",
                "    height: 720",
            ]
        ),
        encoding="utf-8",
    )
    (lanes_dir / "CAM_TEST_lanes.yaml").write_text(
        "\n".join(
            [
                "camera_id: CAM_TEST",
                "lanes:",
                "  - lane_id: lane_a",
                "    name: Lane A",
            ]
        ),
        encoding="utf-8",
    )
    (zones_dir / "CAM_TEST_zones.yaml").write_text(
        json.dumps({"camera_id": "CAM_TEST", "zones": []}),
        encoding="utf-8",
    )

    _create_user("admin", "secret123", "admin")
    _create_user("viewer", "secret123", "viewer")
    _create_user("operator", "secret123", "operator")

    yield {
        "configs_dir": configs_dir,
        "cameras_dir": cameras_dir,
        "lanes_dir": lanes_dir,
        "zones_dir": zones_dir,
        "settings_path": settings_path,
    }

    if hasattr(db_session_module, "reset_engine_cache"):
        db_session_module.reset_engine_cache()
    else:
        db_session_module.get_engine.cache_clear()


@pytest.fixture()
def app(isolated_runtime: dict[str, Path]) -> FastAPI:
    api = FastAPI()
    api.include_router(auth_router)
    api.include_router(cameras_router)
    api.include_router(counts_router)
    api.include_router(dashboard_router)
    api.include_router(reports_router)
    api.include_router(settings_router)
    api.include_router(zones_router)
    return api


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(client: TestClient):
    def _login(username: str = "admin", password: str = "secret123") -> dict[str, str]:
        response = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Login failed for {username}: {response.text}")
        access_token = response.json()["access_token"]
        return {"Authorization": f"Bearer {access_token}"}

    return _login
