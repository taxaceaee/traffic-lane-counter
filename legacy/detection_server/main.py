"""TrafficFlow Detection Server — pure ML inference microservice.

Loads YOLO model(s) and exposes REST + WebSocket endpoints for:
- vehicle detection (bbox + class)
- tracking (track_id)
- lane assignment (lane_id — from caller-sent lane polygons)
- counting line crossings (lane_id, direction)
- occupancy (per-lane vehicle count)

No DB, no file storage, no visualization.  Returns structured JSON only.

The caller (backend server) sends lane config on every request — lanes
can change per camera without restarting the detection server.

Usage::

    uvicorn detection_server.main:app --host 0.0.0.0 --port 8001

Environment variables
---------------------
CORS_ORIGINS : str
    Comma-separated allowed origins (default: "http://localhost:3000").
API_KEY : str (optional)
    When set, /detect endpoints require ``X-API-Key`` header.
"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from detection_server.api.routes_detect import router as detect_router
from detection_server.api.routes_health import router as health_router
from detection_server.api.routes_stream import router as stream_router

logger = logging.getLogger("detection_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

API_KEY = os.getenv("API_KEY", "")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logger.info("Detection server starting (API_KEY %s)", "set" if API_KEY else "not set")
    yield
    from detection_server.api.routes_stream import manager
    manager.stop_all(timeout=15.0)
    logger.info("Detection server shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="TrafficFlow Detection Server",
        version="0.3.0",
        description="Pure ML inference microservice — YOLO + ByteTrack + lane + counting",
        lifespan=lifespan,
    )

    origins_raw = os.getenv("CORS_ORIGINS", "http://localhost:3000")
    origins = [o.strip() for o in origins_raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if API_KEY:
        class APIKeyMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path.startswith("/detect"):
                    key = request.headers.get("X-API-Key", "")
                    if key != API_KEY:
                        return JSONResponse({"detail": "Invalid or missing API key"}, status_code=401)
                return await call_next(request)
        app.add_middleware(APIKeyMiddleware)

    app.include_router(health_router)
    app.include_router(detect_router)
    app.include_router(stream_router)

    return app


app = create_app()
