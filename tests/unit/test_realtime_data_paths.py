"""Guards: operational SPA + queries stay on real live/DB data, not demo fixtures."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tf_db.models import VehicleCountEvent
from tf_db.repositories import DEMO_JOB_IDS, SqlQueryRepository
from tf_db.session import SessionLocal

# Pages that must never invent fleet/traffic metrics in the browser.
_OPS_PAGES = (
    "dashboard.js",
    "live.js",
    "counting.js",
    "events.js",
    "reports.js",
    "health.js",
)

# Patterns that would fabricate traffic numbers client-side (allow Math for UI layout only).
_FORBIDDEN = re.compile(
    r"""
    \bMath\.random\b
    | \bfake[A-Z_]\w*
    | \bsynthetic[A-Z_]\w*
    | \bmockTraffic\b
    | \bdemoCounts\b
    | \bhardcoded\b
    | \bSAMPLE_EVENTS\b
    | \bDUMMY_\w+
    """,
    re.IGNORECASE | re.VERBOSE,
)

_API_HINT = re.compile(r"apiRequest\s*\(|/api/|/live/|/ws/")


def test_ops_spa_pages_bind_to_api_not_synthetic_generators():
    root = Path("services/frontend/js/pages")
    for name in _OPS_PAGES:
        path = root / name
        text = path.read_text(encoding="utf-8")
        assert path.exists(), f"missing {path}"
        assert _API_HINT.search(text), f"{name} must call API/WS"
        bad = _FORBIDDEN.findall(text)
        assert not bad, f"{name} contains forbidden synthetic metric patterns: {bad}"


def test_seed_cli_not_imported_by_api_runtime():
    """API package must not pull in seed_db (boot stays free of fake traffic)."""
    api_root = Path("packages/tf_api")
    for path in api_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "seed_db" not in text, f"{path} references seed_db"
        assert "scripts.seed" not in text, f"{path} imports seed tooling"


def test_query_repository_excludes_seed_job_ids(isolated_runtime):
    session = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        session.add_all(
            [
                VehicleCountEvent(
                    camera_id="CAM_RT",
                    job_id="seed",
                    lane_id="lane_1",
                    track_id=1,
                    vehicle_type="car",
                    direction="forward",
                    confidence=0.9,
                    frame_id=1,
                    created_at=now - timedelta(minutes=1),
                ),
                VehicleCountEvent(
                    camera_id="CAM_RT",
                    job_id="live-CAM_RT",
                    lane_id="lane_1",
                    track_id=2,
                    vehicle_type="motorcycle",
                    direction="forward",
                    confidence=0.88,
                    frame_id=2,
                    created_at=now - timedelta(seconds=30),
                ),
            ]
        )
        session.commit()

        repo = SqlQueryRepository(session)
        assert "seed" in DEMO_JOB_IDS
        total = repo.get_counts_total(camera_id="CAM_RT")
        assert total == 1
        recent = repo.get_recent_events("CAM_RT", limit=10)
        assert len(recent) == 1
        assert recent[0]["job_id"] == "live-CAM_RT"
        assert recent[0]["vehicle_type"] == "motorcycle"

        # Explicit opt-in still sees seed rows (debug only).
        all_repo = SqlQueryRepository(session, exclude_demo_jobs=False)
        assert all_repo.get_counts_total(camera_id="CAM_RT") == 2
    finally:
        session.close()


def test_live_metrics_poll_is_single_cadence_in_frontend():
    """Live page must not dual-poll metrics (stream health + metrics) every few seconds."""
    js = Path("services/frontend/js/pages/live.js").read_text(encoding="utf-8")
    assert "LIVE_METRICS_MS" in js
    assert "stopLivePage" in js
    assert "startLiveMetricsPolling" in js
    # Dual-poll regression: two independent setInterval hitting /metrics is banned.
    # Only one metrics interval should be created in startLiveMetricsPolling.
    assert js.count("setInterval(tick, LIVE_METRICS_MS)") == 1
    assert "_monitorStream" not in js or "function _monitorStream" not in js
