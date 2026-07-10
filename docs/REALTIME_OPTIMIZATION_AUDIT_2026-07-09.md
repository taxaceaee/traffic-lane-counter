# Realtime Optimization Audit - 2026-07-09

## Muc tieu

Audit lai cac thanh phan active cua project theo huong realtime:

- backend API
- live streaming / websocket
- worker pipeline
- database / persistence
- frontend live dashboard
- repo layout / phan du thua co the gay nham lan khi deploy

Checklist nay la baseline de fix theo tung buoc, uu tien cac duong nong anh huong truc tiep den FPS, latency, frame freshness va tinh on dinh 24/7.

## Findings

### 1. Live pipeline / MJPEG

1. `tf_api/api/routes_live.py`
   - Dang luu `_last_full_frames[camera_id] = frame.copy()` tren moi frame du snapshot route khong can doc lien tuc. Day la 1 ban sao full-frame moi iteration, rat ton RAM va CPU.
   - Mapping toa do ROI -> original dang lap lai 2 nhanh `if/elif` voi cung logic, vua thua vua de sai khi maintain.
   - `crop_data` dang encode JPEG crop ngay trong thread capture/live. Khi co nhieu crossing trong 1 frame, chi phi encode crop chen thang vao duong nong detection.
   - `stream_meta["source_fps"]` co, nhung output pacing phu thuoc queue/maxsize=1 + browser consumption. Can track output fps ro rang hon va giam backlog encode.

2. `tf_worker/io/video_io.py`
   - `YouTubeLiveReader._capture_loop()` gap read fail la `break` thread ngay. Sau do route live phai reconnect ca pipeline, gay hut/quang stream khong can thiet.
   - Refresh HLS URL co, nhung reader live chua co co che reconnect noi bo nhe nhu RTSP reader.

### 2. WebSocket / Live event fan-out

1. `tf_api/api/routes_ws.py`
   - `_ensure_redis_listener()` tra ve shared task theo channel, nhung moi client disconnect lai `cancel()` task do trong `finally`. Client roi som co the kill shared listener cua client khac. Day la bug logic nghiem trong.
   - `broadcast_raw()` duyet `_connections` va check `any(c in key for c in cameras)` theo substring, de match sai camera id.
   - Async websocket layer dang vua dung Redis listener, vua dung `AsyncLiveEventBus`, nhung chua quan ly subscription refcount.

2. `tf_common/live_bus.py`
   - `AsyncLiveEventBus._handler()` duoc goi tu thread pipeline, nhung day vao `asyncio.Queue` bang `put_nowait()` truc tiep, khong qua `loop.call_soon_threadsafe`. Day la diem race/thread-safety khong an toan, co the mat event hoac loi ngam.

### 3. Storage / Database

1. `tf_worker/storage/storage_worker.py`
   - `enqueue(..., crop_bytes=...)` nhan `crop_bytes` nhung khong dua vao `payload`; ket qua `_process()` luon doc `payload.get("crop_bytes")` la `None`. Nghia la crop khong bao gio duoc persist du caller da encode. Day la bug logic ro rang.
   - Khi `db_breaker.call(insert_event, ...)` fail, `_dead_letter.append(payload)` co luc khong di qua lock, khong nhat quan voi noi khac.
   - SQLite aggregate fallback trong repo dang `SELECT ... WITH FOR UPDATE ...` cho tung bucket, moi event la nhieu query; voi realtime co the rat cham.

2. `tf_db/repositories.py`
   - `_upsert_fallback()` cho SQLite/Postgres-khong-native dang la vong lap read-then-update/insert tung bucket. Can toi uu theo dialect, it nhat voi SQLite phai dung `INSERT ... ON CONFLICT DO UPDATE` neu co san.
   - `get_latest_occupancy()` dung event 60 giay gan nhat de suy ra occupancy. Cach nay co the hop cho UI tong quan, nhung khong thuc su phan anh trang thai occupancy hien tai neu xe dung yen lau > 60s. Can giu nguyen neu chua co bang occupancy state, nhung phai note ro.

### 4. Frontend live page

1. `frontend/js/pages/live.js`
   - Khi co `lane_change_event`, frontend lai goi API `/lane-changes?limit=5` moi lan event ve. Neu traffic cao se tao polling theo event, rat lang phi.
   - `_setProtectedImage()` fetch blob snapshot moi lan reload stream state; co revoke URL nhung van nen tranh lap lai neu camera khong doi.
   - `loadLiveCameraData()` re-init stream + polling + websocket moi lan chon cam, nhung khong co guard tranh duplicate mot vai timer khi UI thao tac nhanh.

2. `frontend/js/core.js`
   - `loadPage()` them `?t=Date.now()` tren moi lan switch tab, vo hieu hoa browser cache HTML pages. Khi dieu huong trong SPA se ton request khong can thiet.

### 5. Repo / deploy clarity

1. Repo active/legacy da tach kha ro, nhung van can:
   - giu docs active mo ta luong deploy backend/frontend/worker/database rieng;
   - tranh de cac file lock tam thoi trong `configs/*/*.lock` bi hieu nham la can commit/deploy;
   - bo sung file audit nay lam moc tracking cho toi uu realtime.

## Thu tu fix de an toan

1. Fix bug logic mat crop + toi uu DB/storage payload.
2. Fix websocket listener sharing + thread-safe in-process event bridge.
3. Toi uu live pipeline de giam copy/encode thua.
4. Toi uu frontend live de giam request/thao tac du thua.
5. Re-test: unit/integration + smoke live endpoints.

## Implementation status

Da fix trong dot nay:

- `tf_worker/storage/storage_worker.py`
  - dua `crop_bytes` vao payload queue;
  - dong nhat dead-letter locking.
- `tf_db/repositories.py`
  - toi uu SQLite aggregate upsert bang `INSERT ... ON CONFLICT DO UPDATE`.
- `tf_common/live_bus.py`
  - sua bridge thread -> asyncio bang `loop.call_soon_threadsafe(...)`.
- `tf_api/api/routes_ws.py`
  - chuyen Redis listener sang refcounted shared subscription;
  - sua exact camera matching khi broadcast.
- `tf_api/api/routes_live.py`
  - bo full-frame copy moi frame cho snapshot cache;
  - thay bang snapshot JPEG refresh dinh ky;
  - rut gon logic restore bbox ROI.
- `tf_api/api/routes_cameras.py`
  - snapshot route tai su dung snapshot cache moi.
- `tf_worker/io/video_io.py`
  - `YouTubeLiveReader` co reconnect noi bo thay vi vo thread doc frame ngay khi read fail.
- `frontend/js/pages/live.js`
  - render lane-change truc tiep tu websocket event, khong fetch lai API moi event;
  - tranh fetch lai snapshot blob neu URL khong doi.
- `frontend/js/core.js`
  - bo cache-busting `Date.now()` tren moi lan load page.

Con lai, chua xu ly trong dot nay nhung khong chan runtime active:

- backlog Ruff/lint cu tren nhieu module ngoai pham vi realtime;
- occupancy latest van la xap xi dua tren event 60 giay gan nhat, can thiet ke occupancy-state store rieng neu muon "true occupancy" 24/7.

## Tieu chi verify sau khi fix

- API van list dung 3 camera YouTube duoc chi dinh.
- `StorageWorker` persist duoc crop/event va khong vo logic queue.
- WebSocket nhieu client cung subscribe khong bi mat listener khi 1 client disconnect.
- Live metrics `source_fps`, `process_fps`, `output_fps` hop ly va frontend hien thi on dinh.
- Frontend live khong spam API theo moi event.
- Test suite active pass, compile khong loi, smoke endpoint live/metrics/ws ok.
