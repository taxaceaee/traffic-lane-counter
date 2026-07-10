"""Auth API — JWT-based login, refresh, logout backed by DB users."""

import logging
import os
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

logger = logging.getLogger("trafficflow.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])
security = HTTPBearer(auto_error=False)

_DEFAULT_SECRET = "trafficflow_dev_secret_key_not_for_prod"  # noqa: S105 - explicit dev sentinel
SECRET_KEY = os.getenv("JWT_SECRET", _DEFAULT_SECRET)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7
_INSECURE_SECRETS = {
    _DEFAULT_SECRET,
    "trafficflow_local_dev_secret_change_me",
    "CHANGE_ME_IN_PRODUCTION",
}


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - protocol response value, not a secret
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str


def is_unsafe_default_secret() -> bool:
    return SECRET_KEY in _INSECURE_SECRETS or len(SECRET_KEY) < 32


def assert_auth_configuration() -> None:
    app_env = os.getenv("APP_ENV", "development").lower()
    if app_env in {"production", "staging"} and is_unsafe_default_secret():
        raise RuntimeError("JWT_SECRET must be a unique high-entropy secret in staging/production.")


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid.uuid4()),
        "type": "access",
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": str(uuid.uuid4()),
        "type": "refresh",
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token, return a compact user payload."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access" or not payload.get("sub"):
            raise HTTPException(401, "Invalid access token")

        from tf_db.repositories import SqlUserRepository
        from tf_db.session import SessionLocal

        session = SessionLocal()
        try:
            user = SqlUserRepository(session).get_user_model(payload["sub"])
            if not user or not user.is_active:
                raise HTTPException(401, "User is inactive or no longer exists")
            if int(payload.get("ver", 0)) != int(user.token_version or 0):
                raise HTTPException(401, "Token has been revoked")
            return {"sub": user.username, "role": user.role}
        finally:
            session.close()
    except HTTPException:
        raise
    except (JWTError, TypeError, ValueError):
        raise HTTPException(401, "Invalid or expired token") from None


def get_current_user(cred: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    """Extract and validate JWT, return user payload."""
    if cred is None:
        raise HTTPException(401, "Missing authorization header")
    return decode_access_token(cred.credentials)


def require_admin(user: dict = Depends(get_current_user)) -> None:
    """Dependency: require admin role."""
    if user.get("role") not in ("admin", "Administrator"):
        raise HTTPException(403, "Admin role required")


def _require_roles(user: dict, roles: Iterable[str]) -> dict:
    if user.get("role") not in set(roles):
        raise HTTPException(403, "Insufficient permissions")
    return user


def require_operator(user: dict = Depends(get_current_user)) -> dict:
    return _require_roles(user, ("admin", "Administrator", "operator", "Operator"))


def _hash_password(password: str) -> str:
    from passlib.hash import bcrypt

    return bcrypt.hash(password)


def _verify_password(password: str, password_hash: str) -> bool:
    from passlib.hash import bcrypt

    return bcrypt.verify(password, password_hash)


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

        payload = {
            "sub": user.username,
            "role": user.role,
            "ver": int(user.token_version or 0),
        }
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
        if payload.get("type") != "refresh" or not payload.get("sub"):
            raise HTTPException(status_code=401, detail="Invalid token type")
        username = payload.get("sub")

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
            if int(payload.get("ver", 0)) != int(user.get("token_version", 0)):
                raise HTTPException(status_code=401, detail="Refresh token revoked")
        finally:
            session.close()

        return TokenResponse(
            access_token=create_access_token({
                "sub": username,
                "role": user["role"],
                "ver": int(user.get("token_version", 0)),
            }),
            refresh_token=create_refresh_token({
                "sub": username,
                "role": user["role"],
                "ver": int(user.get("token_version", 0)),
            }),
            user={"username": username, "role": user["role"]},
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from None


@router.post("/logout")
async def logout(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    """Revoke all current access and refresh tokens for the user."""
    if cred is None:
        raise HTTPException(401, "Missing authorization header")
    user = decode_access_token(cred.credentials)

    from tf_db.repositories import SqlUserRepository
    from tf_db.session import SessionLocal

    session = SessionLocal()
    try:
        if not SqlUserRepository(session).revoke_tokens(
            user["sub"], datetime.now(timezone.utc)
        ):
            raise HTTPException(401, "User not found")
    finally:
        session.close()
    return {"detail": "Logged out"}
