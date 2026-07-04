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

- **`trafficflow/`** — AI core (detection, tracking, counting). Protocol-based storage abstraction — no server imports.
- **`trafficflow_server/`** — FastAPI backend, WebSocket, auth, DB repositories.
- **`trafficflow_dashboard/`** — Streamlit dashboard.
- **`deploy/`** — Docker Compose stack + Nginx config.

## Quick Start

```bash
# 1. Install dependencies
pip install -e .

# 2. Run pipeline on a video file
python run_occupancy.py --source data/video.mp4 --config configs/mvi_40864_config.yaml --output-dir outputs/demo

# 3. Start API server
uvicorn trafficflow_server.main:app --host 0.0.0.0 --port 8000

# 4. Or run the full stack with Docker
cp deploy/.env.example deploy/.env
docker compose -f deploy/docker-compose.yml up --build
```

## API Overview

| Method | Endpoint | Auth | Rate Limit | Description |
|--------|----------|------|------------|-------------|
| POST | `/api/infer/video` | API key | 10/m | Submit video inference job → 202 + `Location` |
| GET | `/api/jobs` | API key | — | List jobs |
| GET | `/api/jobs/{id}` | API key | — | Job details |
| GET | `/api/jobs/{id}/files` | API key | — | Job output files |
| GET | `/api/cameras` | API key | — | List cameras |
| GET | `/api/cameras/{id}` | API key | — | Camera details |
| GET | `/api/cameras/{id}/lanes` | API key | — | Lane configuration |
| PUT | `/api/cameras/{id}/lanes` | API key | 10/m | Update lanes |
| GET | `/api/cameras/{id}/occupancy` | API key | — | Occupancy time series |
| GET | `/api/cameras/{id}/lane-changes` | API key | — | Lane change events |
| GET | `/api/models` | API key | — | List models |
| GET | `/api/health` | Public | — | `{"status":"ok"}` |
| GET | `/api/metrics` | API key | — | Metrics summary |
| GET | `/api/admin/health` | API key | — | Detailed health (GPU, DB) |
| GET | `/api/admin/metrics` | API key | — | Detailed metrics |
| WS | `/ws/live` | First-message auth | 10/m | Live event stream |

## Key Features

- **Counting lines** with configurable direction and half-plane tint
- **Lane occupancy** with smoothing and unknown timeout
- **Per-lane vehicle counts** with confidence filtering
- **Live mode** for RTSP streams with Redis Pub/Sub
- **Graceful shutdown** on SIGTERM/SIGINT
- **Atomic DB ingestion** with dialect-aware UPSERT batch rollups
- **Rate limiting** at both Nginx (30r/s) and application (60/m) layers
- **Constant-time API key comparison** via `hmac.compare_digest`

## Verification

```bash
ruff check .

# Path traversal resistance
python -c "from trafficflow.io.safe_path import safe_join; safe_join(Path('/tmp/base'), '/tmp/base_evil/x')"
```

## Project Structure

```
├── trafficflow/              AI core package
│   ├── counting/             Line crossing counter
│   ├── detection/            YOLO detector wrapper
│   ├── tracking/             ByteTrack adapter
│   ├── lanes/                Lane assignment
│   ├── occupancy/            Lane state manager, occupancy engine
│   ├── visualization/        Frame annotation / overlay
│   ├── storage/              Protocol interfaces + StorageWorker
│   ├── io/                   Config loader, video I/O, writers
│   ├── evaluation/           DETRAC metrics, XML parsing
│   ├── benchmark/            Benchmark runner
│   └── config/               Server-side Pydantic schemas + compiler
├── trafficflow_server/       FastAPI backend
│   ├── api/                  Route handlers
│   ├── db/                   SQLAlchemy models + repositories
│   ├── services/             Business logic
│   └── schemas/              Pydantic request/response models
├── trafficflow_dashboard/    Streamlit dashboard
├── deploy/                   Docker Compose + Nginx + Dockerfiles
├── configs/                  YAML configs (app, models, cameras, lanes)
├── tests/                    Test suite
└── outputs/                  Pipeline outputs (video, JSONL, CSV)
```

## License

MIT
