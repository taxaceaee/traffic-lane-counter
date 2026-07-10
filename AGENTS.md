# AGENTS.md - Trạng thái hiện tại của `vehicle_counting` (cập nhật 2026-07-09)

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
| Legacy code | `legacy/` | chỉ giữ tham khảo, không dùng cho runtime mới |

## Những nguyên tắc hiện tại

1. Không sửa vào `legacy/` khi triển khai tính năng mới.
2. Không dùng lại cây `backend/`, `shared/`, `detection_server/` cũ.
3. Frontend không còn bundle `frontend/js/app.js`; chỉ dùng:
   - `frontend/js/core.js`
   - `frontend/js/pages/*.js`
4. Backend có thể chạy độc lập với frontend bằng `SERVE_FRONTEND=false`.
5. Settings defaults/reset do backend làm nguồn thật duy nhất:
   - `GET /api/settings`
   - `GET /api/settings/defaults`
   - `POST /api/settings/reset`
6. Camera YouTube live chỉ dùng đúng 3 source đã khóa trong `tf_api/api/routes_cameras.py`.

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

- `scripts/benchmark_model.py` - benchmark FPS model cục bộ
- `scripts/run_benchmark.py` - benchmark pipeline/report
- `scripts/import_jsonl.py` - import output JSONL vào DB
- `scripts/seed_db.py` - seed dữ liệu demo

## Ghi chú dọn repo

- Nếu một file/script chỉ còn phục vụ migration cũ, chuyển sang `legacy/`.
- Nếu một tài liệu generated không còn bám runtime hiện tại, xóa thay vì giữ snapshot stale.
- Weight mặc định nằm trong `weights/`, không để file model rơi ở repo root.
