# AGENTS.md - TrafficFlow monorepo (cập nhật 2026-07-11)

## Layout

```
packages/          # Python libs (import names unchanged)
  tf_api/          # FastAPI backend code
  tf_worker/       # inference pipeline / worker
  tf_core/         # detection, tracking, lanes, occupancy
  tf_common/       # logging, redis, yt utils, metrics
  tf_db/           # ORM, repositories, session

services/          # Deploy units (one folder = one deployable)
  backend/         # Dockerfile + README (API)
  frontend/        # SPA static + Dockerfile
  model/           # Dockerfile + README (YOLO worker)
  database/        # Alembic migrations + README

deploy/            # Full-stack compose + nginx
configs/           # cameras, lanes, zones, models, settings
weights/           # model weights (.pt gitignored)
scripts/           # seed, serve_frontend, import_jsonl, run_occupancy
tests/
```

## Runtime mapping

| Deploy unit | Code | Notes |
|---|---|---|
| Backend API | `packages/tf_api` | FastAPI, auth, jobs, live, ws, settings |
| Model worker | `packages/tf_worker` + `tf_core` | detection, tracking, counting |
| Database | `packages/tf_db` + `services/database` | models + Alembic |
| Frontend SPA | `services/frontend` | `index.html`, `js/`, `pages/` |
| Shared | `packages/tf_common` | logging, alerts, yt utils |

## Principles

1. Frontend only: `services/frontend/js/core.js` + `pages/*.js` (no `app.js` bundle).
2. Backend can run without SPA: `SERVE_FRONTEND=false`.
3. Settings defaults/reset: backend is source of truth (`/api/settings*`).
4. Camera YouTube live: locked sources in `tf_api` cameras routes.
5. Live always-on: `AUTO_START_LIVE_STREAMS=true` (see `.env.example`).

## Local entrypoints

```bash
# API + optional SPA
./start.sh

# Frontend only
API_BASE_URL=http://localhost:8000 .venv/bin/python -m scripts.serve_frontend

# Offline worker
.venv/bin/python -m tf_worker.worker

# Full stack
docker compose -f deploy/docker-compose.yml up --build
```

## Checks after changes

```bash
export PYTHONPATH=packages
.venv/bin/python -m compileall packages scripts tests
.venv/bin/python -m pytest tests -q
```

## Scripts

- `scripts/serve_frontend.py` — SPA local server
- `scripts/seed_db.py` — demo data
- `scripts/import_jsonl.py` — import JSONL → DB
- `scripts/run_occupancy.py` — offline pipeline job

## Notes

- Do not keep DETRAC/benchmark stacks.
- Weights in `weights/` only (not repo root).
- Import names stay `tf_api`, `tf_worker`, … — only filesystem layout changed.
