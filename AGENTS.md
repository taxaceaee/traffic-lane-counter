# AGENTS.md - Trạng thái hiện tại của `vehicle_counting` (cập nhật 2026-07-11)

## Runtime active

| Thành phần | Đường dẫn chính | Ghi chú |
|---|---|---|
| Backend API | `tf_api/` | FastAPI, auth, jobs, live, ws, settings |
| AI core | `tf_core/` | detection, tracking, lane/counting, occupancy |
| Worker | `tf_worker/` | pipeline, storage worker, video IO |
| Database layer | `tf_db/` | models, repositories, session |
| Shared runtime | `tf_common/` | logging, alerts, safe path, yt utils |
| Frontend SPA | `frontend/` | `index.html`, `js/core.js`, `js/pages/*.js` |
| Deploy assets | `deploy/` | tách theo `api/`, `frontend/`, `worker/`, `proxy/`, `stack/` |

## Những nguyên tắc hiện tại

1. Frontend không còn bundle `frontend/js/app.js`; chỉ dùng:
   - `frontend/js/core.js`
   - `frontend/js/pages/*.js`
2. Backend có thể chạy độc lập với frontend bằng `SERVE_FRONTEND=false`.
3. Settings defaults/reset do backend làm nguồn thật duy nhất:
   - `GET /api/settings`
   - `GET /api/settings/defaults`
   - `POST /api/settings/reset`
4. Camera YouTube live chỉ dùng đúng 3 source đã khóa trong `tf_api/api/routes_cameras.py`.
5. Live detection always-on (mặc định):
   - `AUTO_START_LIVE_STREAMS=true` (xem `.env.example`) — API boot auto-start
     mọi camera YAML, supervisor restart mỗi 30s.
   - Không cần mở Live Monitoring để Dashboard/Events có data.
   - `GET /live/status` — fleet overview (running / always_on / fps).
   - Tắt: `AUTO_START_LIVE_STREAMS=false` (chỉ start khi có MJPEG viewer).

## Local entrypoints

```bash
# API local
./start.sh

# Frontend local
API_BASE_URL=http://localhost:8000 .venv/bin/python -m scripts.serve_frontend

# Worker local
.venv/bin/python -m tf_worker.worker

# Full stack local
docker compose -f deploy/stack/docker-compose.yml up --build
```

## Kiểm tra nhanh sau mỗi cụm thay đổi

```bash
.venv/bin/python -m compileall tf_api tf_common tf_core tf_db tf_worker scripts tests
.venv/bin/python -m pytest tests -q
```

## Utility scripts còn giữ lại

- `scripts/serve_frontend.py` - SPA local server
- `scripts/seed_db.py` - seed dữ liệu demo
- `scripts/import_jsonl.py` - import output JSONL vào DB
- `scripts/run_occupancy.py` - chạy 1 job pipeline offline

## Ghi chú dọn repo

- Không giữ DETRAC/benchmark stack trong runtime tree.
- Không commit audit generated / `test-reports/`.
- Weight mặc định nằm trong `weights/`, không để file model rơi ở repo root.
