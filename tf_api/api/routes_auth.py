"""Auth API — JWT-based login, refresh, logout backed by DB users."""

import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

logger = logging.getLogger("trafficflow.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])
security = HTTPBearer(auto_error=False)

SECRET_KEY = os.getenv("JWT_SECRET", "trafficflow_dev_secret_key_not_for_prod")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(cred: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    """Extract and validate JWT, return user payload."""
    if cred is None:
        raise HTTPException(401, "Missing authorization header")
    try:
        payload = jwt.decode(cred.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        return {"sub": payload.get("sub"), "role": payload.get("role")}
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")


def require_admin(user: dict = Depends(get_current_user)) -> None:
    """Dependency: require admin role."""
    if user.get("role") not in ("admin", "Administrator"):
        raise HTTPException(403, "Admin role required")


def _hash_password(password: str) -> str:
    try:
        from passlib.hash import bcrypt
        return bcrypt.hash(password)
    except ImportError:
        return password


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        from passlib.hash import bcrypt
        return bcrypt.verify(password, password_hash)
    except ImportError:
        return password == password_hash


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest):
    from tf_db.repositories import SqlUserRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        repo = SqlUserRepository(session)
        user = repo.get_user_model(req.username)
        if not user or not _verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account deactivated")

        repo.update_last_login(req.username, datetime.now(timezone.utc))

        # Audit log
        from tf_db.repositories import SqlAuditRepository
        audit_repo = SqlAuditRepository(session)
        audit_repo.add_entry({
            "user_id": user.id, "username": user.username,
            "action": "login", "resource": "auth",
            "detail": "Login success", "ip_address": "",
        })

        payload = {"sub": user.username, "role": user.role}
        return TokenResponse(
            access_token=create_access_token(payload),
            refresh_token=create_refresh_token(payload),
            user={"username": user.username, "role": user.role},
        )
    finally:
        session.close()


@router.post("/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest):
    try:
        payload = jwt.decode(req.refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        username = payload.get("sub")
        role = payload.get("role", "viewer")

        from tf_db.repositories import SqlUserRepository
        from tf_db.session import SessionLocal

        session = SessionLocal()
        try:
            repo = SqlUserRepository(session)
            user = repo.get_by_username(username)
            if not user:
                raise HTTPException(status_code=401, detail="User not found")
            if not user.get("is_active"):
                raise HTTPException(status_code=403, detail="Account deactivated")
        finally:
            session.close()

        return TokenResponse(
            access_token=create_access_token({"sub": username, "role": role}),
            refresh_token=create_refresh_token({"sub": username, "role": role}),
            user={"username": username, "role": role},
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")


@router.post("/logout")
async def logout():
    return {"detail": "Logged out"}
