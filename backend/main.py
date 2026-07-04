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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from backend.api.routes_auth import router as auth_router
from backend.api.routes_alerts import router as alert_router
from backend.api.routes_cameras import router as cameras_router
from backend.api.routes_counts import router as counts_router
from backend.api.routes_dashboard import router as dashboard_router
from backend.api.routes_detect import router as detect_router
from backend.api.routes_health import router as health_router
from backend.api.routes_jobs import router as jobs_router
from backend.api.routes_lanes import router as lanes_router
from backend.api.routes_live import router as live_router
from backend.api.routes_models import router as models_router
from backend.api.routes_reports import router as reports_router
from backend.api.routes_ws import router as ws_router
from backend.api.routes_zones import router as zones_router
from backend.api.routes_admin import router as admin_router
from backend.api.routes_settings import router as settings_router
from backend.api.routes_users import router as users_router
from backend.api.routes_audit import router as audit_router
from backend.log_setup import setup_logging
from backend.monitoring import metrics

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
    import os as _os
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
    logger.info("Detection server starting (API_KEY=%s, memory_threshold=%.0f MB)", 
                "set" if API_KEY else "not set", _MEMORY_THRESHOLD / 1e6 if _MEMORY_THRESHOLD else 0)
    # Auto-create tables (idempotent — only creates missing ones)
    from backend.db.init_db import create_tables
    create_tables()
    # Seed default admin user if no users exist
    from backend.db.session import SessionLocal
    from backend.db.repositories import SqlUserRepository
    from backend.api.routes_auth import _hash_password
    _seed_session = SessionLocal()
    try:
        _user_repo = SqlUserRepository(_seed_session)
        if not _user_repo.get_by_username("admin"):
            import uuid
            from datetime import datetime, timezone
            _user_repo.create({
                "id": str(uuid.uuid4()),
                "username": "admin",
                "email": "admin@trafficflow.local",
                "password_hash": _hash_password("admin"),
                "role": "admin",
                "is_active": True,
                "created_at": datetime.now(timezone.utc),
            })
            logger.info("Default admin user created (admin:admin)")
    finally:
        _seed_session.close()
    # Start background live metrics poller (GPU, memory, Prometheus)
    from backend.monitoring.live_metrics import start_background_polling
    start_background_polling()
    # Start system-wide health metrics collector (CPU, RAM, disk, GPU, services)
    from backend.monitoring.system_metrics import start_collector
    start_collector()
    # Preload models into GPU memory for instant switching
    from shared.detection.yolo_detector import ModelRegistry
    ModelRegistry.preload("configs/models.yaml")
    # Register WebSocket broadcast callback for alerts
    from backend.services.alert_service import alert_service as _alert_svc
    from backend.api.routes_ws import broadcast_raw as _ws_broadcast
    _alert_svc.register_callback(lambda alert: _ws_broadcast({
        "type": "alert",
        "camera_id": alert.get("camera_id"),
        "data": alert,
    }))
    yield
    logger.info("Detection server shutting down")
    # Release GPU models on shutdown
    from shared.detection.yolo_detector import ModelRegistry
    ModelRegistry.clear()


def _rate_limit_key(request: Request) -> str:
    """Per-API-Key rate limiting — one noisy client cannot starve others."""
    key = request.headers.get("X-API-Key", "")
    if key:
        return f"apikey:{key}"
    client = request.client
    return f"ip:{client.host}" if client else "unknown"


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
                    if key != API_KEY:
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

    # Serve frontend SPA — every page has its own clean URL (/live, /counting, ...)
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi import HTTPException
    import os as _os

    frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")

    if _os.path.isdir(frontend_dir):
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
            file_path = _os.path.join(frontend_dir, full_path)
            response = None
            if full_path and _os.path.isfile(file_path):
                response = _FR(file_path)
            elif full_path in _VALID_SPA_ROUTES:
                response = _FR(_os.path.join(frontend_dir, "index.html"))
            else:
                raise HTTPException(status_code=404)
            # Prevent browser caching of frontend assets during development
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            return response

    # ── Admin routes ──────────────────────────────────────────────────────

    @app.get("/api/admin/metrics", response_class=PlainTextResponse)
    @limiter.limit("10/minute")
    async def admin_metrics(request: Request):
        body, status = metrics.metrics_endpoint()
        return Response(content=body, media_type="text/plain", status_code=status)

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
