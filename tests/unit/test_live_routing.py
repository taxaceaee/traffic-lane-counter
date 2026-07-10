from pathlib import Path


def test_nginx_routes_live_api_before_frontend_fallback():
    config = Path("deploy/proxy/nginx.conf").read_text(encoding="utf-8")
    live_pos = config.index("location /live/")
    frontend_pos = config.index("location / {")
    assert live_pos < frontend_pos
    assert "proxy_pass          http://api_backend/live/;" in config


def test_live_redis_messages_use_frontend_event_envelopes():
    source = Path("tf_api/api/routes_live.py").read_text(encoding="utf-8")
    assert '"type": "occupancy_update"' in source
    assert '"type": "count_event"' in source
    assert '"type": "lane_change_event"' in source
    assert '"previous_lane_id": event.get(' in source
    assert '"current_lane_id": event.get(' in source
    assert "publisher.publish_live_state(camera_id, live_message)" in source
    assert "publisher.publish_live_state(camera_id, event_message)" in source
