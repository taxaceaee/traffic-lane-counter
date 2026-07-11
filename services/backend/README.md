# Backend (API)

FastAPI + WebSocket server: auth, cameras, live MJPEG, dashboard, settings.

## Local

```bash
# from repo root, after ./start.sh bootstrap
.venv/bin/python -m uvicorn tf_api.main:app --host 0.0.0.0 --port 8000
```

Or: `./start.sh` (serves API + optional SPA).

## Docker

```bash
docker build -f services/backend/Dockerfile -t trafficflow-api .
docker run --rm -p 8000:8000 \
  -e JWT_SECRET=change-me \
  -e DATABASE_URL=postgresql://... \
  -e REDIS_HOST=... \
  -e SERVE_FRONTEND=false \
  -e CORS_ORIGINS=https://your-frontend.example \
  trafficflow-api
```

## Depends on

| Service | Why |
|---|---|
| **database** (Postgres) | events, users, aggregates |
| **Redis** | live pub/sub (optional local, required in compose) |
| **model** | offline jobs; live can run in-process on API |

## Key env

- `DATABASE_URL`, `JWT_SECRET`, `REDIS_HOST`, `CORS_ORIGINS`
- `SERVE_FRONTEND=false` when frontend is a separate host
- `AUTO_START_LIVE_STREAMS` — always-on detection on API process
