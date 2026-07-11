"""TrafficFlow Detection Server — unified API for detection and management.

Production hardening:
- Structured logging (LOG_FORMAT=json)
- Prometheus metrics at /api/admin/metrics
- Dependency health check at /api/health
- Per-API-Key rate limiting
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from tf_api.api.routes_admin import router as admin_router
from tf_api.api.routes_alerts import router as alert_router
from tf_api.api.routes_audit import router as audit_router
from tf_api.api.routes_auth import (
    assert_auth_configuration,
    is_unsafe_default_secret,
    require_admin,
)
from tf_api.api.routes_auth import router as auth_router
from tf_api.api.routes_cameras import router as cameras_router
from tf_api.api.routes_counts import router as counts_router
from tf_api.api.routes_dashboard import router as dashboard_router
from tf_api.api.routes_detect import router as detect_router
from tf_api.api.routes_health import router as health_router
from tf_api.api.routes_jobs import router as jobs_router
from tf_api.api.routes_lanes import router as lanes_router
from tf_api.api.routes_live import router as live_router
from tf_api.api.routes_models import router as models_router
from tf_api.api.routes_reports import router as reports_router
from tf_api.api.routes_settings import router as settings_router
from tf_api.api.routes_users import router as users_router
from tf_api.api.routes_ws import router as ws_router
from tf_api.api.routes_zones import router as zones_router
from tf_common.log_setup import setup_logging
from tf_common.monitoring import metrics

# Setup structured logging once
setup_logging()

logger = logging.getLogger("trafficflow_server")

API_KEY = os.getenv("API_KEY", "")

# Auto-restart memory threshold (bytes, 0 = disabled)
_MEMORY_THRESHOLD = int(os.getenv("WORKER_MEMORY_THRESHOLD", "0"))
_last_memory_warning = 0.0


def get_api_key() -> str:
    return API_KEY


def _check_memory() -> str | None:
    """Return warning message if RSS exceeds threshold, else None."""
    if _MEMORY_THRESHOLD <= 0:
        return None
    try:
        import psutil
        proc = psutil.Process()
        rss = proc.memory_info().rss
        global _last_memory_warning
        now = time.time()
        if rss > _MEMORY_THRESHOLD and now - _last_memory_warning > 300:
            _last_memory_warning = now
            logger.critical("Memory threshold exceeded: %.1f MB > %.1f MB", rss / 1e6, _MEMORY_THRESHOLD / 1e6)
            return f"RSS {rss / 1e6:.1f} MB exceeds threshold {_MEMORY_THRESHOLD / 1e6:.1f} MB"
    except ImportError:
        pass
    return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    assert_auth_configuration()
    logger.info("Detection server starting (API_KEY=%s, memory_threshold=%.0f MB)",
                "set" if API_KEY else "not set", _MEMORY_THRESHOLD / 1e6 if _MEMORY_THRESHOLD else 0)
    if is_unsafe_default_secret():
        logger.warning("JWT_SECRET is using the development fallback. Set JWT_SECRET before sharing this environment.")
    # Apply versioned schema changes; do not mutate production schema via create_all.
    from tf_db.init_db import migrate
    migrate()
    logger.info("Database migrations applied")
    # Seed bootstrap admin only when explicitly requested via env.
    from tf_api.api.routes_auth import _hash_password
    from tf_db.repositories import SqlUserRepository
    from tf_db.session import SessionLocal
    _seed_session = SessionLocal()
    try:
        _user_repo = SqlUserRepository(_seed_session)
        bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "").strip()
        bootstrap_username = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin").strip() or "admin"
        if bootstrap_password and not _user_repo.get_by_username(bootstrap_username):
            import uuid
            from datetime import datetime, timezone
            _user_repo.create({
                "id": str(uuid.uuid4()),
                "username": bootstrap_username,
                "email": f"{bootstrap_username}@trafficflow.local",
                "password_hash": _hash_password(bootstrap_password),
                "role": "admin",
                "is_active": True,
                "created_at": datetime.now(timezone.utc),
            })
            logger.info("Bootstrap admin user created: %s", bootstrap_username)
    finally:
        _seed_session.close()
    logger.info("Bootstrap user check complete")
    # Start background live metrics poller (GPU, memory, Prometheus)
    from tf_common.monitoring.live_metrics import start_background_polling
    start_background_polling()
    logger.info("Live metrics poller started")
    # Start system-wide health metrics collector (CPU, RAM, disk, GPU, services)
    from tf_api.monitoring.system_metrics import start_collector
    start_collector()
    logger.info("System metrics collector started")
    # Preload models into GPU memory for instant switching
    from tf_core.detection.yolo_detector import ModelRegistry
    ModelRegistry.preload("configs/models.yaml")
    logger.info("Model preload check complete")
    # Register WebSocket broadcast callback for alerts
    from tf_api.api.routes_ws import broadcast_raw_sync as _ws_broadcast
    from tf_common.alert_service import alert_service as _alert_svc
    _alert_svc.register_callback(lambda alert: _ws_broadcast({
        "type": "alert",
        "camera_id": alert.get("camera_id"),
        "data": alert,
    }))
    # Auto-start always-on live detection for every camera YAML so Dashboard /
    # Events / occupancy keep receiving realtime data without opening Live UI.
    try:
        from tf_api.api.routes_live import start_live_supervisor, stop_live_supervisor
        start_live_supervisor()
        logger.info("Live always-on supervisor requested (AUTO_START_LIVE_STREAMS)")
    except Exception:
        logger.exception("Failed to start live always-on supervisor")
    yield
    logger.info("Detection server shutting down")
    try:
        from tf_api.api.routes_live import (
            _cleanup_stream,
            _streams,
            stop_live_supervisor,
        )
        stop_live_supervisor()
        for camera_id in list(_streams.keys()):
            try:
                _cleanup_stream(camera_id)
            except Exception:
                logger.warning("Live stream cleanup failed for %s during shutdown", camera_id, exc_info=True)
    except Exception:
        logger.warning("Failed to run live stream shutdown cleanup", exc_info=True)
    # Release GPU models on shutdown
    from tf_core.detection.yolo_detector import ModelRegistry
    ModelRegistry.clear()


def _rate_limit_key(request: Request) -> str:
    """Per-API-Key rate limiting — one noisy client cannot starve others."""
    key = request.headers.get("X-API-Key", "")
    if key:
        return f"apikey:{key}"
    client = request.client
    return f"ip:{client.host}" if client else "unknown"


def _should_serve_frontend() -> bool:
    return os.getenv("SERVE_FRONTEND", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def create_app() -> FastAPI:
    limiter = Limiter(
        key_func=_rate_limit_key,
        default_limits=["200/minute"],
        storage_uri=os.getenv("RATE_LIMIT_STORAGE", "memory://"),
    )

    app = FastAPI(
        title="TrafficFlow Detection Server",
        version="0.2.0",
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # CORS
    origins_raw = os.getenv("CORS_ORIGINS", "http://localhost:3000")
    origins = [o.strip() for o in origins_raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(SlowAPIMiddleware)

    # API key middleware for detection + admin routes
    if API_KEY:
        from starlette.middleware.base import BaseHTTPMiddleware

        _PROTECTED_PREFIXES = ("/detect", "/api/admin")

        class APIKeyMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path.startswith(_PROTECTED_PREFIXES):
                    key = request.headers.get("X-API-Key", "")
                    if not compare_digest(key, API_KEY):
                        return JSONResponse(
                            {"detail": "Invalid or missing API key"},
                            status_code=401,
                        )
                return await call_next(request)

        app.add_middleware(APIKeyMiddleware)

    # Include all routers
    app.include_router(health_router)
    app.include_router(detect_router)
    app.include_router(live_router)
    app.include_router(auth_router)
    app.include_router(cameras_router)
    app.include_router(counts_router)
    app.include_router(dashboard_router)
    app.include_router(lanes_router)
    app.include_router(jobs_router)
    app.include_router(models_router)
    app.include_router(reports_router)
    app.include_router(ws_router)
    app.include_router(alert_router)
    app.include_router(admin_router)
    app.include_router(settings_router)
    app.include_router(users_router)
    app.include_router(audit_router)
    app.include_router(zones_router)

    # Register API routes before the SPA catch-all below. FastAPI matches
    # routes in declaration order, so an API endpoint added after the
    # catch-all would incorrectly be returned as a frontend 404.
    @app.get("/api/admin/metrics", response_class=PlainTextResponse)
    @limiter.limit("10/minute")
    async def admin_metrics(request: Request, _user: dict = Depends(require_admin)):
        body, status = metrics.metrics_endpoint()
        return Response(content=body, media_type="text/plain", status_code=status)

    # Serve frontend SPA — every page has its own clean URL (/live, /counting, ...)
    from fastapi import HTTPException
    frontend_dir = (Path(__file__).resolve().parent.parent / "frontend").resolve()

    if _should_serve_frontend() and frontend_dir.is_dir():
        # Whitelist of valid client-side SPA routes
        _VALID_SPA_ROUTES = frozenset([
            '',          # /
            'live',      # /live
            'counting',  # /counting
            'alerts',    # /alerts
            'cameras',   # /cameras
            'lanes',     # /lanes
            'jobs',      # /jobs
            'models',    # /models
            'analytics', # /analytics
            'events',    # /events
            'reports',   # /reports
            'health',    # /health
            'users',     # /users
            'settings',  # /settings
        ])

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _serve_frontend(full_path: str):
            """Serve static files and SPA fallback for clean URLs.

            Registered LAST so API routes take priority. Only serves actual
            files and whitelisted SPA routes — everything else gets 404.
            """
            from fastapi.responses import FileResponse as _FR
            file_path = (frontend_dir / full_path).resolve()
            try:
                file_path.relative_to(frontend_dir)
            except ValueError:
                raise HTTPException(status_code=404) from None
            if full_path and file_path.is_file():
                response = _FR(str(file_path))
            elif full_path in _VALID_SPA_ROUTES:
                response = _FR(str(frontend_dir / "index.html"))
            else:
                raise HTTPException(status_code=404)
            # Prevent browser caching of frontend assets during development
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

    # ── Memory check middleware ───────────────────────────────────────────
    if _MEMORY_THRESHOLD > 0:
        from starlette.middleware.base import BaseHTTPMiddleware

        class MemoryCheckMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                warning = _check_memory()
                response = await call_next(request)
                if warning:
                    response.headers["X-Worker-Memory-Warning"] = warning
                return response

        app.add_middleware(MemoryCheckMiddleware)

    return app


app = create_app()
