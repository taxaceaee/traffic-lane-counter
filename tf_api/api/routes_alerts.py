"""Alerts API — live alerts with real-time WebSocket broadcast."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from tf_api.api.routes_auth import get_current_user, require_operator

logger = logging.getLogger("trafficflow.alerts_api")

router = APIRouter(prefix="/api/alerts", tags=["alerts"])
@router.get("")
async def get_active_alerts(_user: dict = Depends(get_current_user)):
    """Return all active (unresolved) alerts, newest first."""
    from tf_common.alert_service import alert_service
    return alert_service.get_active()


@router.get("/history")
async def get_alert_history(
    limit: int = 100,
    _user: dict = Depends(get_current_user),
):
    """Return alert history (resolved + active), newest first."""
    from tf_common.alert_service import alert_service
    return alert_service.get_history(limit=limit)


@router.get("/count")
async def get_alert_count(
    _user: dict = Depends(get_current_user),
):
    """Return the count of active alerts."""
    from tf_common.alert_service import alert_service
    return {"count": alert_service.get_active_count()}


@router.patch("/{alert_id}/resolve")
async def resolve_alert(
    alert_id: str,
    _user: dict = Depends(require_operator),
):
    """Resolve a specific alert by id."""
    from tf_common.alert_service import alert_service
    if not alert_service.resolve_by_id(alert_id):
        raise HTTPException(404, f"Alert not found or already resolved: {alert_id}")
    return {"status": "resolved", "alert_id": alert_id}
