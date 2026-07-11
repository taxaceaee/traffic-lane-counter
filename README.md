# TrafficFlow — Vision-based Vehicle Counting

Real-time per-lane traffic flow analysis using YOLO detection, ByteTrack tracking, lane assignment, and line-crossing logic.

## Architecture

```
Camera/Video ──► YOLOv11 ──► ByteTrack ──► Lane Assigner ──► Line Counter ──► Occupancy Engine
                      │            │              │                │                │
                      └────── Pipeline ───────────────────────────┘────────────────┘
                                          │
                                    StorageWorker
                                    ┌────┴────┐
                                    │         │
                                  Redis     PostgreSQL
                                    │         │
                                  WS API    REST API
                                    │         │
                                  Dashboard  Nginx
```

- **`tf_core/`** — AI core (detection, tracking, counting).
- **`tf_api/`** — FastAPI backend, WebSocket, auth, monitoring.
- **`tf_worker/`** — inference worker and pipeline jobs.
- **`tf_db/`** — database models and repositories.
- **`frontend/`** — standalone SPA assets.
- **`deploy/`** — service-by-service deployment assets.

## Deployment Surfaces

This repo is now organized around the pieces you can deploy independently:

- `tf_api/` + `deploy/api/` - backend API service
- `frontend/` + `deploy/frontend/` - frontend SPA service
- `tf_worker/` + `deploy/worker/` - AI worker service
- `tf_db/` + `deploy/stack/.env.example` - database contract and stack env
- `deploy/proxy/` - optional reverse proxy
- `deploy/stack/` - local full-stack compose for integration testing

## Quick Start

```bash
# 1. Install dependencies
python3 -m pip install -e ".[dev]"

# 2. Start API + SPA (local development)
npm run dev
# Login (fresh DB): admin / admin123
# If port 8000 is busy, development mode picks the next free port.

# 3. Optional: standalone frontend
API_BASE_URL=http://localhost:8000 .venv/bin/python -m scripts.serve_frontend

# 4. Optional: offline pipeline job on a video file
python3 -m scripts.run_occupancy \
  --source /path/to/video.mp4 \
  --config configs/cameras/YT_LIVE_TEST.yaml \
  --output-dir outputs/demo

# 5. Full stack with Docker
cp deploy/stack/.env.example deploy/stack/.env
docker compose -f deploy/stack/docker-compose.yml up --build
```

Set `SERVE_FRONTEND=false` when deploying the backend separately from the SPA.

## API Overview

| Method | Endpoint | Auth | Rate Limit | Description |
|--------|----------|------|------------|-------------|
| POST | `/api/infer/video` | JWT operator/admin | 10/m | Submit video inference job → 202 + `Location` |
| GET | `/api/jobs` | JWT | — | List jobs |
| GET | `/api/jobs/{id}` | JWT | — | Job details |
| GET | `/api/cameras` | JWT | — | List cameras |
| GET | `/api/cameras/{id}` | JWT | — | Camera details |
| GET | `/api/cameras/{id}/lanes` | JWT | — | Lane configuration |
| PUT | `/api/cameras/{id}/lanes` | JWT operator/admin | 10/m | Update lanes |
| GET | `/api/cameras/{id}/occupancy` | JWT | — | Occupancy time series |
| GET | `/api/cameras/{id}/lane-changes` | JWT | — | Lane change events |
| GET | `/api/models` | JWT | — | List models |
| GET | `/api/health` | Public | — | `{"status":"ok"}` |
| GET | `/api/admin/health` | JWT admin + API key | — | Detailed health (GPU, DB) |
| GET | `/api/admin/metrics` | JWT admin + API key | — | Detailed metrics |
| WS | `/ws/live` | First-message auth | 10/m | Live event stream |

## Key Features

- **Counting lines** with configurable direction and half-plane tint
- **Lane occupancy** with smoothing and unknown timeout
- **Per-lane vehicle counts** with confidence filtering
- **Live mode** for RTSP/YouTube streams with Redis Pub/Sub
- **Graceful shutdown** on SIGTERM/SIGINT
- **Atomic DB ingestion** with dialect-aware UPSERT batch rollups
- **Rate limiting** at both Nginx (30r/s) and application (60/m) layers
- **Constant-time API key comparison** via `hmac.compare_digest`

## Verification

```bash
.venv/bin/python -m ruff check .

# Path traversal resistance
python3 -c "from tf_common.safe_path import safe_join; from pathlib import Path; safe_join(Path('/tmp/base'), '/tmp/base_evil/x')"
```

## Project Structure

```
├── tf_api/                   Backend API service
├── tf_worker/                Inference worker service
├── tf_db/                    Database layer
├── tf_core/                  Shared CV/AI core
├── tf_common/                Cross-service runtime utilities
├── frontend/                 Standalone SPA bundle
├── deploy/
│   ├── api/                  Backend Dockerfile
│   ├── frontend/             Frontend Dockerfile + runtime config
│   ├── worker/               Worker Dockerfile
│   ├── proxy/                Optional reverse proxy config
│   └── stack/                Full local compose stack
├── configs/                  Runtime YAML/JSON configs
├── weights/                  Model weights used by API/worker runtime
├── scripts/                  Operational scripts
├── tests/                    Integration + unit tests
└── legacy/                   Archived code not used in current runtime
```

## License

MIT
