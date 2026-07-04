"""Users API — DB-backed user management. Admin-only mutations."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from tf_api.api.routes_auth import get_current_user, require_admin, _hash_password

logger = logging.getLogger("trafficflow.users")

router = APIRouter(prefix="/api/users", tags=["users"])


class CreateUserRequest(BaseModel):
    username: str
    email: str = ""
    password: str
    role: str = "viewer"


class UpdateUserRequest(BaseModel):
    email: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


def _audit(user_id, username, action, resource="", detail="", ip=""):
    try:
        from tf_db.repositories import SqlAuditRepository
        from tf_db.session import SessionLocal
        session = SessionLocal()
        try:
            repo = SqlAuditRepository(session)
            repo.add_entry({
                "user_id": user_id, "username": username,
                "action": action, "resource": resource,
                "detail": detail, "ip_address": ip,
            })
        finally:
            session.close()
    except Exception:
        logger.warning("Failed to write audit entry", exc_info=True)


@router.get("")
async def list_users(_user: dict = Depends(get_current_user)):
    from tf_db.repositories import SqlUserRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        repo = SqlUserRepository(session)
        return repo.list_users()
    finally:
        session.close()


@router.get("/me")
async def get_current_user_info(user: dict = Depends(get_current_user)):
    from tf_db.repositories import SqlUserRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        repo = SqlUserRepository(session)
        u = repo.get_by_username(user["sub"])
        if not u:
            raise HTTPException(404, "User not found")
        return u
    finally:
        session.close()


@router.post("", status_code=201)
async def create_user(body: CreateUserRequest,
                      _user: dict = Depends(require_admin)):
    if body.role not in ("admin", "operator", "viewer"):
        raise HTTPException(400, f"Invalid role: {body.role}")

    from tf_db.repositories import SqlUserRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        repo = SqlUserRepository(session)
        if repo.get_by_username(body.username):
            raise HTTPException(409, f"Username already exists: {body.username}")

        user_data = {
            "id": str(uuid.uuid4()),
            "username": body.username,
            "email": body.email,
            "password_hash": _hash_password(body.password),
            "role": body.role,
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        }
        result = repo.create(user_data)
        _audit(_user["sub"], _user["sub"], "user_create",
               f"users/{body.username}", f"Created {body.role}: {body.username}")
        return result
    finally:
        session.close()


@router.put("/{user_id}")
async def update_user(user_id: str, body: UpdateUserRequest,
                       _user: dict = Depends(require_admin)):
    from tf_db.repositories import SqlUserRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        repo = SqlUserRepository(session)
        old = repo.get_by_id(user_id)
        if not old:
            raise HTTPException(404, "User not found")

        updates = {}
        if body.email is not None:
            updates["email"] = body.email
        if body.role is not None:
            if body.role not in ("admin", "operator", "viewer"):
                raise HTTPException(400, f"Invalid role: {body.role}")
            updates["role"] = body.role
        if body.is_active is not None:
            updates["is_active"] = body.is_active
        if body.password:
            updates["password_hash"] = _hash_password(body.password)

        updated = repo.update(user_id, updates)
        if not updated:
            raise HTTPException(404, "User not found")

        detail_parts = [f"{k}={v}" for k, v in updates.items() if k != "password_hash"]
        if "password_hash" in updates:
            detail_parts.append("password=changed")
        _audit(_user["sub"], _user["sub"], "user_update",
               f"users/{user_id}", f"Updated: {', '.join(detail_parts)}")
        return updated
    finally:
        session.close()


@router.delete("/{user_id}")
async def delete_user(user_id: str, _user: dict = Depends(require_admin)):
    from tf_db.repositories import SqlUserRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        repo = SqlUserRepository(session)
        old = repo.get_by_id(user_id)
        if not old:
            raise HTTPException(404, "User not found")
        if old["username"] == "admin":
            raise HTTPException(400, "Cannot delete the primary admin user")

        repo.update(user_id, {"is_active": False})
        _audit(_user["sub"], _user["sub"], "user_delete",
               f"users/{user_id}", f"Deactivated user: {old['username']}")
        return {"status": "deactivated", "user_id": user_id}
    finally:
        session.close()
