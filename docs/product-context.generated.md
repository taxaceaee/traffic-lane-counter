# Product Context — TrafficFlow Vehicle Counting

## Overview
TrafficFlow is a vision-based per-lane vehicle counting system using YOLO detection, ByteTrack tracking, lane assignment, and line-crossing logic. It provides real-time traffic analytics via a FastAPI backend, WebSocket events, and a Streamlit/SPA dashboard.

## Users & Roles
| Role | Capabilities | Authentication |
|---|---|---|
| Anonymous | `/api/health` only | None |
| API Client | All `/api/cameras`, `/api/jobs`, `/api/models`, `/api/infer` | `X-API-Key` header |
| Authenticated User | Login, refresh, WebSocket live events | JWT (access + refresh token) |
| Admin | `/api/admin/health`, `/api/admin/metrics` | JWT + admin role |

## Environments
| Environment | DB | Redis | GPU | Purpose |
|---|---|---|---|---|
| Development/Test | SQLite in-memory | Optional | Optional | Local dev, unit tests |
| Production | PostgreSQL | Required | Required | Real traffic analysis |

## Critical Workflows
1. **Frame processing pipeline**: Capture → YOLO detection → ByteTrack → Lane assign → Line counter → Occupancy
2. **Event persistence**: Crossing event → StorageWorker (queue) → batch commit → PostgreSQL
3. **Live streaming**: RTSP capture → MJPEG generator → HTTP streaming
4. **WebSocket events**: Auth → heartbeat → live crossing events push
5. **Lane configuration**: CRUD on lane polygons → atomic YAML write with file lock
6. **Inference job**: Video upload → processing job → output files

## Sensitive Data
| Data Class | Location | Exposure |
|---|---|---|
| Camera video frames | Pipeline, queue, MJPEG stream | Internal network |
| API keys | Environment variable, request headers | Backend middleware |
| JWT tokens | Auth headers, localStorage | Transient |
| DB connection strings | Environment variable | Server-side only |
| Lane config files | Filesystem (`configs/lanes/`) | Internal |

## External Dependencies
| Dependency | Failure Mode | Mitigation |
|---|---|---|
| PostgreSQL | Connection loss → 503 API, dropped events | Circuit breaker, batch retry |
| Redis | Pub/sub unavailable | Graceful degradation |
| GPU/CUDA | OOM → crash | ModelRegistry singleton |
| RTSP cameras | Stream drop → no frames | Exponential backoff reconnection |
| YouTube live | Stream unavailable | Retry with backoff |
