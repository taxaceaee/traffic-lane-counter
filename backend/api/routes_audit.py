"""Audit Log API — DB-backed, JWT-protected audit trail."""

import logging

from fastapi import APIRouter, Depends, Query

from backend.api.routes_auth import get_current_user

logger = logging.getLogger("trafficflow.audit")

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
async def list_audit_logs(
    limit: int = Query(50, ge=1, le=500),
    user_id: str | None = None,
    action: str | None = None,
    _user: dict = Depends(get_current_user),
):
    """List audit log entries. Requires authentication."""
    from backend.db.repositories import SqlAuditRepository
    from backend.db.session import SessionLocal

    session = SessionLocal()
    try:
        repo = SqlAuditRepository(session)
        return repo.list_entries(limit=limit, user_id=user_id, action=action)
    finally:
        session.close()
