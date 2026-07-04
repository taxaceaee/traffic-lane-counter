# AGENTS.md - Trạng thái hệ thống `vehicle_counting` (cập nhật 2026-07-04)

## Trạng thái hiện tại

| Thành phần | Trạng thái |
|---|---|
| Backend API (FastAPI) | Hoàn chỉnh: auth, cameras, lanes, jobs, models, detect, health, live, ws |
| Frontend (SPA dashboard) | Hoàn chỉnh: 14 pages with clean URLs, split JS modules, lane canvas, charts |
| AI Pipeline (DetectionCore) | Hoạt động: YOLO + ByteTrack + Lane + Occupancy + Counting |
| StorageWorker | Hoạt động với backpressure monitoring, dead-letter queue |
| WebSocket | Auth + heartbeat + connection limits |
| Database | SQLite/Postgres, pool_pre_ping, pool_recycle |
| Tests | 486 passed, 1 failed (YouTube rate-limit), 1 skipped |

## Frontend SPA Routing (Clean URLs)

| URL | Page | JS Module |
|---|---|---|
| `/` | Dashboard | `js/pages/dashboard.js` |
| `/live` | Live Monitoring | `js/pages/live.js` |
| `/counting` | Vehicle Counting | `js/pages/counting.js` |
| `/alerts` | Alert System | `js/pages/alerts.js` |
| `/cameras` | Camera Management | `js/pages/cameras.js` |
| `/lanes` | Lane Configuration | `js/pages/lanes.js` |
| `/jobs` | Inference Jobs | `js/pages/jobs.js` |
| `/models` | Model Management | `js/pages/models.js` |
| `/analytics` | Traffic Analytics | `js/pages/analytics.js` |
| `/events` | Lane-change Events | `js/pages/events.js` |
| `/reports` | Reports | `js/pages/reports.js` |
| `/health` | System Health | `js/pages/health.js` |
| `/users` | Users & Audit | `js/pages/users.js` (placeholder) |
| `/settings` | Settings | `js/pages/settings.js` |

Shared core: `js/core.js` (routing, API client, state management)

To rebuild combined `app.js` for production:
```bash
cat frontend/js/core.js frontend/js/pages/*.js > frontend/js/app.js
```

## Backend SPA Serving
- Catch-all `GET /{path}` route registered LAST after all API routers
- Serves actual files from `frontend/` directory
- Returns `index.html` for whitelisted SPA routes
- Returns 404 for unknown paths (security: prevents path traversal leaks)

## API Endpoints

| Method | Path | Mô tả |
|---|---|---|
| POST | /api/auth/login | JWT login |
| POST | /api/auth/refresh | Refresh token |
| POST | /api/auth/logout | Logout |
| GET | /api/health | Public health check (safe, no sensitive info) |
| GET | /api/cameras | List cameras |
| GET | /api/cameras/{id} | Camera detail |
| GET | /api/cameras/{id}/lanes | Get lane config |
| PUT | /api/cameras/{id}/lanes | Update lane config (atomic write + file lock) |
| GET | /api/cameras/{id}/occupancy | Occupancy history |
| GET | /api/cameras/{id}/lane-changes | Lane change events |
| POST | /api/infer/video | Submit inference job (max 4 concurrent) |
| GET | /api/jobs | List jobs |
| GET | /api/jobs/{id} | Job detail |
| GET | /api/jobs/{id}/video | Job output video |
| GET | /api/models | List models |
| POST | /detect/frame | Single frame detection |
| WS | /detect/stream | Streaming detection |
| GET | /live/{id}/stream.mjpg | MJPEG live stream (auto-reconnect) |
| WS | /ws/live | Live events (auth required) |

## 24/7 Multi-Camera Operational Hardening

### 1. File-Level Locking cho Lane Config Writes
- **File:** `backend/api/routes_lanes.py`
- **Vấn đề:** Concurrent PUT requests có thể ghi đè hoặc corrupt file YAML
- **Giải pháp:** `fcntl.flock` exclusive lock + atomic temp-file replacement (`write + replace`)

### 2. Thread-Safe Camera Stream Management
- **File:** `backend/api/routes_live.py`
- **Vấn đề:** Nhiều MJPEG consumer truy cập cùng camera stream
- **Giải pháp:** `threading.Lock` bảo vệ `_streams` dict, cleanup stale streams sau 5 phút, max 16 concurrent streams

### 3. Camera Auto-Reconnection với Exponential Backoff
- **File:** `backend/api/routes_live.py`
- **Vấn đề:** RTSP stream dropout, camera reboot
- **Giải pháp:** Auto-reconnect với backoff: 1s, 2s, 4s, ... 60s max, tối đa 10 lần

### 4. StorageWorker Backpressure & Dead-Letter Queue
- **File:** `backend/storage/storage_worker.py`
- **Vấn đề:** Queue đầy → silent drop events
- **Giải pháp:** 
  - Backpressure warning at 512/2048
  - Critical drop at 1024/2048 → dead-letter queue (max 10k)
  - Graceful shutdown: drain trước khi stop
  - Watchdog: log + theo dõi queue depth

### 5. Database Connection Pool Management
- **File:** `backend/db/session.py`
- **Vấn đề:** Connection leak, stale connections, no pool limits
- **Giải pháp:** 
  - `pool_pre_ping=True`: validate before use
  - `pool_recycle=1800`: 30 min recycle
  - `pool_size=10, max_overflow=5`: bounded pool
  - Auto `rollback` on exception trong generator

### 6. Job Concurrency Limits
- **File:** `backend/api/routes_jobs.py`
- **Vấn đề:** Unlimited concurrent jobs → OOM, GPU exhaustion
- **Giải pháp:**
  - Max 4 concurrent inference jobs
  - Lock-protected active job registry
  - Auto-cleanup completed/failed jobs sau 7 ngày
  - 429 Too Many Requests khi vượt limit

### 7. WebSocket Connection Limits
- **File:** `backend/api/routes_ws.py`
- **Vấn đề:** Unlimited WS connections → memory leak, DDoS
- **Giải pháp:**
  - Max 5 connections per user
  - Max 500 global connections
  - Heartbeat every 30s, timeout 90s
  - Auth token required, 10s auth window

### 8. Alert Service
- **File:** `backend/services/alert_service.py`
- **Vấn đề:** Không có cơ chế thông báo khi system gặp sự cố
- **Giải pháp:** Centralized alert với callback registry, suppress duplicate alerts, in-memory history (last 1000)

### 9. Memory Management
- **Frame buffer:** StorageWorker queue giới hạn 2048 items, payload chỉ chứa crop bytes (không full frame)
- **Job retention:** Tự động cleanup jobs sau 7 ngày
- **Alert history:** Giới hạn 1000 alerts
- **Dead-letter queue:** Giới hạn 10k items

### 10. Security Hardening
- **S1:** `safe_join()` dùng `Path.relative_to()` containment check
- **S2:** CORS đọc từ env, reject wildcard `*`
- **S3:** WebSocket yêu cầu JWT token authentication
- **S4:** Public health chỉ trả `{"status":"ok"}`, không leak device/GPU/DB info
- **API Key:** Middleware bảo vệ `/detect` và `/api/admin` routes
- **Rate limiting:** slowapi với default 200/minute

## Frontend Lane Drawing Canvas
- Visual canvas-based lane polygon editor
- Camera snapshot as background
- Click-to-draw polygon, undo, clear
- Counting line editor (start, end, direction ref)
- Save to server via PUT /api/cameras/{id}/lanes
- Color-coded lanes, live preview

## CI Commands
```bash
ruff check .
```
