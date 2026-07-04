"""Alerts API — live alerts with real-time WebSocket broadcast."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("trafficflow.alerts_api")

router = APIRouter(prefix="/api/alerts", tags=["alerts"])
security = HTTPBearer(auto_error=False)


def _auth_user(cred: HTTPAuthorizationCredentials | None) -> str | None:
    """Return username if token is valid, else None."""
    if cred is None:
        return None
    try:
        from jose import JWTError, jwt
        from tf_api.api.routes_auth import SECRET_KEY, ALGORITHM
        payload = jwt.decode(cred.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


@router.get("")
async def get_active_alerts(
    cred: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Return all active (unresolved) alerts, newest first."""
    username = _auth_user(cred)
    if username is None:
        raise HTTPException(401, "Invalid or missing token")
    from tf_common.alert_service import alert_service
    return alert_service.get_active()


@router.get("/history")
async def get_alert_history(
    limit: int = 100,
    cred: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Return alert history (resolved + active), newest first."""
    username = _auth_user(cred)
    if username is None:
        raise HTTPException(401, "Invalid or missing token")
    from tf_common.alert_service import alert_service
    return alert_service.get_history(limit=limit)


@router.get("/count")
async def get_alert_count(
    cred: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Return the count of active alerts."""
    username = _auth_user(cred)
    if username is None:
        raise HTTPException(401, "Invalid or missing token")
    from tf_common.alert_service import alert_service
    return {"count": alert_service.get_active_count()}


@router.patch("/{alert_id}/resolve")
async def resolve_alert(
    alert_id: str,
    cred: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Resolve a specific alert by id."""
    username = _auth_user(cred)
    if username is None:
        raise HTTPException(401, "Invalid or missing token")
    from tf_common.alert_service import alert_service
    if not alert_service.resolve_by_id(alert_id):
        raise HTTPException(404, f"Alert not found or already resolved: {alert_id}")
    return {"status": "resolved", "alert_id": alert_id}
