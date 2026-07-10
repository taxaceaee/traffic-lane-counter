"""Alert service — monitors system health and generates alerts for 24/7 operations.

Alert types:
- camera_offline: stream connection lost
- queue_backpressure: StorageWorker queue filling up
- job_failed: inference job failed
- high_occupancy: lane occupancy > 80%
- db_connection_failed: database unreachable
- disk_space_low: storage running out
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("trafficflow.alert")


class AlertService:
    """Central alert service for 24/7 system health monitoring.

    Collects alerts from all system components and broadcasts them
    via registered callbacks (e.g., WebSocket broadcast, Slack webhook).
    """

    def __init__(self):
        self._alerts: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._callbacks: list[Callable] = []
        self._suppressed: dict[str, str] = {}  # dedup_key → alert_id

    def register_callback(self, cb: Callable) -> None:
        """Register a callback invoked on every new alert."""
        self._callbacks.append(cb)

    def emit(
        self,
        severity: str,
        title: str,
        message: str,
        camera_id: str | None = None,
        alert_type: str = "general",
        suppress_if_active: bool = True,
    ) -> str | None:
        """Emit an alert. Returns the alert id, or None if suppressed.

        suppress_if_active prevents duplicate alerts of same type+camera.
        """
        dedup_key = f"{alert_type}:{camera_id or 'global'}"
        alert_id = str(uuid.uuid4())

        with self._lock:
            if suppress_if_active and dedup_key in self._suppressed:
                return None

            alert = {
                "id": alert_id,
                "severity": severity,
                "title": title,
                "message": message,
                "camera_id": camera_id,
                "alert_type": alert_type,
                "dedup_key": dedup_key,
                "timestamp": time.time(),
                "resolved": False,
                "resolved_at": None,
            }
            self._alerts.append(alert)
            self._suppressed[dedup_key] = alert_id

            # Keep last 1000 alerts in memory
            if len(self._alerts) > 1000:
                self._alerts = self._alerts[-1000:]

        # Notify callbacks
        for cb in self._callbacks:
            try:
                cb(alert)
            except Exception:
                logger.exception("Alert callback failed")

        return alert_id

    def resolve(self, alert_type: str, camera_id: str | None = None) -> bool:
        """Resolve (clear suppression) for a given alert type.
        Returns True if an active alert was resolved.
        """
        dedup_key = f"{alert_type}:{camera_id or 'global'}"
        now = time.time()
        with self._lock:
            alert_id = self._suppressed.pop(dedup_key, None)
            if alert_id is None:
                return False
            # Mark the alert as resolved in history
            for a in self._alerts:
                if a["id"] == alert_id:
                    a["resolved"] = True
                    a["resolved_at"] = now
                    break
        return True

    def resolve_by_id(self, alert_id: str) -> bool:
        """Resolve a specific alert by id. Returns True if found and resolved."""
        now = time.time()
        with self._lock:
            for a in self._alerts:
                if a["id"] == alert_id and not a["resolved"]:
                    a["resolved"] = True
                    a["resolved_at"] = now
                    self._suppressed.pop(a["dedup_key"], None)
                    return True
        return False

    def get_active(self) -> list[dict[str, Any]]:
        """Return unresolved (active) alerts, newest first."""
        with self._lock:
            return [
                a for a in self._alerts
                if not a["resolved"]
            ][-50:][::-1]

    def get_active_count(self) -> int:
        """Return count of active alerts."""
        with self._lock:
            return sum(1 for a in self._alerts if not a["resolved"])

    def get_history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return alert history (resolved + unresolved), newest first."""
        with self._lock:
            return list(self._alerts[-limit:])[::-1]


# Global singleton
alert_service = AlertService()
