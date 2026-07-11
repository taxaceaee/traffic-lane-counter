from __future__ import annotations

import asyncio
import json
import uuid

from tf_api.api import routes_live
from tf_common.alert_service import AlertService, alert_service
from tf_common.live_errors import diagnose_stream_error
from tf_common.monitoring.live_metrics import get_camera_metrics, record_stream_state
from tf_common.yt_utils import build_ydl_opts


def test_youtube_antibot_error_has_operator_remediation() -> None:
    diagnostic = diagnose_stream_error(
        ValueError("Sign in to confirm you're not a bot"),
        source_type="youtube_live",
        source="https://www.youtube.com/watch?v=example",
    )

    assert diagnostic["code"] == "YOUTUBE_ANTIBOT_BLOCKED"
    assert diagnostic["fix_steps"]
    assert diagnostic["verify_steps"]
    assert "yt-dlp --simulate" in diagnostic["verification_command"]


def test_alert_keeps_structured_diagnostic_details() -> None:
    service = AlertService()
    service.emit(
        "critical",
        "YouTube blocked yt-dlp",
        "Source blocked",
        camera_id="CAM_DIAGNOSTIC",
        alert_type="stream_source_error",
        details={"code": "YOUTUBE_ANTIBOT_BLOCKED", "fix_steps": ["Use cookies"]},
    )

    active = service.get_active()
    assert active[0]["details"]["code"] == "YOUTUBE_ANTIBOT_BLOCKED"
    assert active[0]["details"]["fix_steps"] == ["Use cookies"]


def test_live_metrics_exposes_error_code_and_details() -> None:
    camera_id = "CAM_" + uuid.uuid4().hex[:8]
    diagnostic = {"code": "YOUTUBE_ANTIBOT_BLOCKED", "title": "Blocked"}
    record_stream_state(
        camera_id,
        "reconnecting",
        "YouTube blocked yt-dlp",
        error_code=diagnostic["code"],
        error_details=diagnostic,
    )

    metrics = get_camera_metrics(camera_id)
    assert metrics["status"] == "reconnecting"
    assert metrics["error_code"] == "YOUTUBE_ANTIBOT_BLOCKED"
    assert metrics["error_details"] == diagnostic


def test_verify_source_returns_antibot_diagnostic(monkeypatch) -> None:
    camera_id = "CAM_" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(
        routes_live,
        "_load_camera_config",
        lambda _camera_id: {
            "source": "https://www.youtube.com/watch?v=example",
            "source_type": "youtube_live",
            "input": {"yt_format": "best[height<=720]"},
        },
    )

    def blocked(*_args, **_kwargs):
        raise ValueError("Sign in to confirm you're not a bot")

    import tf_common.yt_utils as yt_utils

    monkeypatch.setattr(yt_utils, "resolve_stream_info", blocked)
    response = asyncio.run(routes_live.verify_live_source(camera_id, _user={}))
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["ok"] is False
    assert payload["diagnostic"]["code"] == "YOUTUBE_ANTIBOT_BLOCKED"


def test_capture_failure_publishes_alert_and_metrics() -> None:
    camera_id = "CAM_" + uuid.uuid4().hex[:8]
    meta = {}
    diagnostic = routes_live._record_source_failure(
        camera_id,
        "youtube_live",
        "https://www.youtube.com/watch?v=example",
        ValueError("Sign in to confirm you're not a bot"),
        stream_meta=meta,
    )

    metrics = get_camera_metrics(camera_id)
    active = [a for a in alert_service.get_active() if a["camera_id"] == camera_id]
    assert diagnostic["code"] == "YOUTUBE_ANTIBOT_BLOCKED"
    assert metrics["error_code"] == "YOUTUBE_ANTIBOT_BLOCKED"
    assert active and active[0]["details"]["code"] == "YOUTUBE_ANTIBOT_BLOCKED"
    assert meta["source_failure_seen"] is True
    alert_service.resolve("stream_source_error", camera_id)


def test_youtube_options_enable_js_ejs_and_parse_browser_profile(monkeypatch) -> None:
    monkeypatch.setenv("YOUTUBE_COOKIES_FROM_BROWSER", "chrome:Default")
    monkeypatch.setenv("YTDLP_REMOTE_COMPONENTS", "ejs:github")

    options = build_ydl_opts()

    assert options["cookiesfrombrowser"][:2] == ("chrome", "Default")
    assert "ejs:github" in options["remote_components"]
    assert "node" in options["js_runtimes"]
