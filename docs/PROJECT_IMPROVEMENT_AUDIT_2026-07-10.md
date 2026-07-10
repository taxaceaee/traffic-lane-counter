# Báo cáo audit và backlog cải thiện `vehicle_counting`

Ngày audit: 2026-07-10  
Phạm vi: runtime mới trong `tf_api/`, `tf_core/`, `tf_worker/`, `tf_db/`, `tf_common/`, `frontend/`, `configs/`, `deploy/`, `.github/` và `tests/`. `legacy/` chỉ được kiểm tra để phân biệt phạm vi, không xem là runtime cần sửa.  
Quyết định hiện tại: **NO-GO cho release production**.

> **Follow-up 2026-07-10:** baseline findings bên dưới được giữ lại để trace
> nguyên nhân. Trạng thái mới nhất nằm ở mục 8. Một số P1/P2 đã được sửa
> trong working tree, nhưng chưa thể tuyên bố production-ready khi chưa có
> full-stack Postgres/Redis, browser E2E và integration HTTP thật.

## 1. Tóm tắt điều hành

Repo đã có một số nền tảng tốt: package runtime đã tách khỏi cây cũ, RBAC cơ bản, `safe_join()` hiện dùng `Path.relative_to()`, lane `counting_line` đã đi qua schema/compiler, rate limiting và atomic lane/zone write đã có, compile Python pass.

Tuy nhiên, chưa nên coi hệ thống là production-ready vì còn các lỗi chặn release:

1. Image API không copy/cài `tf_worker`, trong khi `tf_api/api/routes_jobs.py` import `tf_worker.pipeline` ngay lúc load module. Full-stack image có nguy cơ không khởi động.
2. Offline/video job được đánh dấu `completed` nhưng auto-ingest không `commit()` DB trước khi đóng session; dữ liệu event có thể bị mất hoàn toàn.
3. `/api/dashboard/*` không có dependency auth; dữ liệu tổng hợp và danh sách camera có thể truy cập không cần JWT.
4. `/detect/*` chỉ được bảo vệ khi `API_KEY` được set; sample production không bắt buộc biến này, nên endpoint inference có thể public và gây tiêu thụ CPU/GPU.
5. Job registry chỉ nằm trong memory và inference video chạy trong process API, không phải worker service; restart/scale API làm mất trạng thái và có thể tạo job không được quản lý.
6. WebSocket Redis subscriber dùng cứng `localhost:6379`, không dùng `REDIS_HOST`; trong Compose API container sẽ không kết nối tới service `redis`.
7. JWT lifecycle chưa hoàn chỉnh: refresh token được chấp nhận như access token, logout không revoke token, role trong refresh token được tin lại từ token cũ.
8. Không có revision Alembic nào và startup dùng `Base.metadata.create_all()`, chưa có migration/rollback/backup-restore gate.
9. Chỉ weight `weights/yolo11n.pt` tồn tại, nhưng default worker và nhiều config trỏ tới `yolo11s.pt`; cấu hình mặc định không replay được.
10. Test suite chỉ collect được 15 test; integration đầu tiên treo quá 30 giây, Ruff fail 227 lỗi ở runtime mới, mypy không chạy được do duplicate module `monitoring`.

## 2. Bằng chứng kiểm tra đã chạy

| Kiểm tra | Kết quả | Ghi chú |
|---|---|---|
| `python -m compileall tf_api tf_common tf_core tf_db tf_worker scripts tests` | PASS | Không có syntax error ở phạm vi runtime/test mới |
| `pytest tests --collect-only -q` | PASS | Collect 15 test |
| Unit query test đơn lẻ | PASS | 1 test chạy pass |
| Realtime async bus test đơn lẻ | PASS | 1 test chạy pass |
| Integration test đầu tiên | TIMEOUT | `test_counts_recent_requires_auth` không kết thúc trong 30 giây |
| Full `pytest tests -q` | BLOCKED/FAIL | Không có output sau khi bắt đầu; đã dừng tiến trình ở exit 130 |
| `ruff check tf_api tf_common tf_core tf_db tf_worker scripts tests` | FAIL | 227 lỗi, 62 lỗi có thể auto-fix |
| `ruff check .` | FAIL | 412 lỗi; bao gồm cả `legacy/` |
| `mypy tf_api tf_common tf_core tf_db tf_worker` | FAIL | Duplicate module `monitoring` giữa `tf_api/monitoring` và `tf_common/monitoring` |
| `import tf_api.main` local | PASS | Chỉ chứng minh môi trường local có đủ package; chưa chứng minh API Docker image |
| Kiểm tra `configs/models.yaml` | FAIL | 1/5 model path tồn tại |
| `docker compose ... config --quiet` | BLOCKED | `deploy/stack/.env` chưa tồn tại; cần tạo từ `.env.example` trước |
| Git metadata | FAIL | `HEAD`/branches trỏ tới SHA không tồn tại, `git fsck` báo nhiều object/blob thiếu |

Các gate chưa được chạy vì repo không có môi trường staging/test riêng hoặc chưa có fixture an toàn: browser E2E, DAST, dependency audit, container scan, load/stress, migration test, backup-restore, multi-process WebSocket và model golden regression.

## 3. Findings ưu tiên P1 — phải xử lý trước release

### P1-01 — API image thiếu dependency/runtime `tf_worker`

- Bằng chứng: `tf_api/api/routes_jobs.py:23` import `TrafficFlowPipeline` từ `tf_worker.pipeline`; `tf_api/main.py:25` import router jobs trong lúc tạo app.
- `deploy/api/Dockerfile:11-24` chỉ install/copy `tf_core`, `tf_common`, `tf_db`, `tf_api`, `configs`, `weights`, `scripts`; không có `tf_worker`.
- `tf_api/pyproject.toml:6-18` cũng không khai báo `tf-worker`.
- Impact: API container có thể fail ở import trước khi healthcheck chạy; các route live còn lazy-import thêm module worker.
- Cải thiện: hoặc tách job/live execution ra worker qua queue/API contract, hoặc khai báo/copy/install đầy đủ `tf_worker` và dependencies trong image API. Sau đó phải build image thật và chạy smoke `/api/health`, `/api/auth/login`, `/api/infer/video` trong Compose.
- Tiêu chí đạt: `docker compose build api` pass; container API start ổn trong image sạch; không có import ngầm từ package không được cài.

### P1-02 — Offline job báo thành công nhưng auto-ingest không commit dữ liệu

- Bằng chứng: `tf_api/api/routes_jobs.py:142-160` đánh dấu job `completed` rồi gọi `_auto_ingest()`; `:180-227` insert từng event; `:257-262` chỉ `adapter.close()` và bắt lỗi chung.
- `tf_api/storage_adapters.py:32-34` cho thấy `close()` chỉ đóng session, không commit.
- `tf_db/repositories.py:56-65` cho thấy `insert_event()` chỉ `session.add()`; commit là trách nhiệm caller.
- Impact: video inference có thể trả job completed nhưng event/aggregate không xuất hiện trong dashboard; lỗi ingest còn bị nuốt và không đổi trạng thái job.
- Cải thiện: ingest theo batch transaction, commit rõ ràng, rollback khi lỗi, idempotency key theo `job_id + frame_id + track_id + lane_id`, ghi `ingest_status/error` vào job và chỉ hoàn tất job sau khi persistence thành công.
- Tiêu chí đạt: test chạy ingest trên SQLite/PostgreSQL, kiểm tra row count sau restart và retry; lỗi DB phải hiện là failed/degraded, không phải completed giả.

### P1-03 — Dashboard endpoints thiếu auth

- Bằng chứng: `tf_api/api/routes_dashboard.py:16-20` chỉ tạo router; `:32-33` và `:97-101` không có `Depends(get_current_user)`.
- Test hiện tại còn gọi `/api/dashboard/hourly` không có header ở `tests/integration/test_api_contracts.py:186-191`, nên đang che khuất/duy trì hành vi public.
- Impact: người không đăng nhập có thể đọc tổng số xe, camera IDs, phân bố loại xe và traffic theo giờ.
- Cải thiện: thêm auth dependency hoặc tách rõ endpoint public liveness khỏi dữ liệu dashboard; thêm test anonymous=401, viewer=200, admin/operator theo policy.
- Tiêu chí đạt: toàn bộ route inventory có auth boundary được khai báo và test deny path.

### P1-04 — Endpoint inference `/detect` có thể public

- Bằng chứng: `tf_api/api/routes_detect.py:85-98` và `:105-151` không có JWT dependency.
- `tf_api/main.py:196-213` chỉ chặn prefix `/detect` nếu `API_KEY` khác rỗng; `.env.example` không bắt buộc `API_KEY` và production compose không set biến này.
- Impact: request ảnh hoặc WebSocket inference có thể dùng CPU/GPU và model memory không cần user; dễ bị resource exhaustion, đặc biệt vì không có upload/body size limit rõ ở route.
- Cải thiện: bảo vệ bằng service-to-service credential hoặc JWT role operator/admin; bắt buộc API key mạnh trong deployment; giới hạn body bytes, frame dimensions, model allow-list, concurrent sessions và timeout.
- Tiêu chí đạt: anonymous/invalid key bị từ chối trong mọi environment; test oversized/malformed frame và concurrent session.

### P1-05 — JWT lifecycle cho phép token cũ tiếp tục có quyền

- Bằng chứng:
  - `tf_api/api/routes_auth.py:65-71` `decode_access_token()` không kiểm tra `payload["type"] == "access"`.
  - `:144-173` refresh token lấy `role` từ payload cũ rồi phát access token mới; không dùng role hiện tại trong DB.
  - `:176-178` logout chỉ trả JSON, không revoke access/refresh token.
  - `:74-78` kiểm tra chữ ký/expiry nhưng không kiểm tra user còn active/revoked.
- Impact: refresh token có thể dùng trực tiếp như access token; user bị disable/role hạ cấp vẫn giữ quyền đến khi token hết hạn; logout không có tác dụng server-side.
- Cải thiện: kiểm tra token type/audience/issuer; tra user active và quyền hiện tại cho request quan trọng; lưu refresh-session/token version hoặc denylist có TTL; rotate refresh token và revoke token cũ khi logout/password/role change.
- Tiêu chí đạt: test expired, wrong type, disabled user, role downgrade, logout rồi replay và refresh-token reuse.

### P1-06 — JWT bị đưa vào query string của MJPEG

- Bằng chứng: `frontend/js/pages/live.js:165-181` tạo URL `?access_token=...`; backend nhận tại `tf_api/api/routes_live.py:810-817`.
- Nginx access log ghi request URI tại `deploy/proxy/nginx.conf:39-43`.
- Impact: token có thể xuất hiện trong proxy/access log, browser history, monitoring hoặc referrer; đây là leakage vector đối với token bearer.
- Cải thiện: dùng cookie HttpOnly/SameSite cho browser stream, hoặc cơ chế short-lived one-time stream ticket đổi từ Authorization header; redact query token khỏi logs và không nhận token dài hạn trên URL.
- Tiêu chí đạt: không có bearer token trong URL/log; ticket hết hạn, dùng một lần và bị revoke khi logout.

### P1-07 — WebSocket Redis connection sai trong Compose và thiếu isolation

- Bằng chứng: `tf_api/api/routes_ws.py:228-235` khởi tạo Redis bằng `host="localhost", port=6379`, bỏ qua `REDIS_HOST`/`REDIS_PORT`; Compose khai báo Redis service là `redis` tại `deploy/stack/docker-compose.yml:86-89`.
- Cùng module chỉ xác thực chữ ký token ở `:48-57`, không kiểm tra user active/role/origin; subscription camera ở `:112-120` không giới hạn/validate rõ theo quyền camera.
- `broadcast_raw()` ở `:260-275` gửi tuần tự tới nhiều socket, nên một client chậm có thể làm chậm fan-out.
- Impact: live event liên container không chạy; nếu fallback/global subscription được dùng, user có thể nhận camera ngoài phạm vi policy; fan-out có thể bị backpressure.
- Cải thiện: đọc env thống nhất; tạo Redis client/pool trong lifespan; auth bằng access token hợp lệ + active-user check; allow-list Origin; camera authorization; queue riêng/backpressure/drop policy per client; test nhiều client, disconnect, Redis restart và slow consumer.
- Tiêu chí đạt: hai container API/Redis trao đổi event thật; client A không nhận camera B; disconnect một client không làm mất listener chung.

### P1-08 — Job state chỉ ở memory và inference chạy trong API process

- Bằng chứng: `_JOBS`/`_active_jobs` là dict/set module-level ở `tf_api/api/routes_jobs.py:35-37`; job chạy bằng daemon thread tại `:142-149` và `:268`.
- API route import trực tiếp `tf_worker.pipeline`, thay vì gửi job vào queue worker; không có DB job model/repository hoặc cancel/lease/recovery path.
- Impact: restart API làm mất trạng thái; scale nhiều replica làm mỗi replica có quota riêng và danh sách job khác nhau; daemon thread có thể bị kill khi process dừng; API và inference tranh CPU/RAM/GPU.
- Cải thiện: persist job state trong DB/queue; dùng worker consumer với lease/heartbeat/retry/DLQ; idempotent submit; endpoint cancel; resource quota theo user/camera; API chỉ điều phối.
- Tiêu chí đạt: restart worker/API không làm mất job; một job có trạng thái duy nhất khi chạy nhiều replica; retry không tạo duplicate events.

### P1-09 — DB schema không có migration path hoặc restore evidence

- Bằng chứng: `alembic/` chỉ có `env.py` và `script.py.mako`, không có `alembic/versions/`; `tf_db/init_db.py:6-7` dùng `Base.metadata.create_all()`; `tf_api/main.py:89-90` gọi nó trong lifespan.
- Không tìm thấy backup/restore/rollback script hoặc CI gate; `deploy/database/README.md` chỉ mô tả biến kết nối.
- Impact: thay đổi model không được version hóa; deploy mới không migrate schema cũ, rollback không có kiểm chứng, restore có thể không tương thích app.
- Cải thiện: tạo baseline revision và các revision tiếp theo; chạy `alembic upgrade head` trong release; kiểm tra schema drift; migration forward/rollback policy; backup encrypted và restore vào DB sạch trong CI/staging.
- Tiêu chí đạt: app không tự ý mutate schema production bằng `create_all`; có log migration, backup-restore report và version trace theo release.

### P1-10 — Default model configuration không replay được

- Bằng chứng kiểm tra thực tế: `configs/models.yaml` chỉ resolve được `weights/yolo11n.pt`; `yolo11s.pt`, `yolo11m.pt`, `yolo11l.pt`, `yolo11x.pt` đều missing.
- `deploy/stack/docker-compose.yml:131-132` và `:172-174` mặc định `MODEL_FILE=yolo11s.pt`; `tf_worker/worker.py:51-54` cũng fallback về `yolo11s.pt`; nhiều config `configs/mvi_*` dùng tên này.
- `configs/models.yaml:6-21` còn dùng path không nhất quán: model nano là `weights/yolo11n.pt`, các model còn lại là path ở repo root.
- Impact: worker default có thể fail khi load model; UI/model registry hiển thị model không chạy được; benchmark/release không reproducible.
- Cải thiện: chọn một default tồn tại hoặc cung cấp artifact qua release registry; chuẩn hóa mọi path tương đối theo repo/config root; validate tất cả model trước startup; lưu SHA256/model version.
- Tiêu chí đạt: `worker` startup fail-fast với lỗi rõ nếu thiếu weight; default Compose chạy được trong image sạch; model list chỉ chứa model đã verify.

### P1-11 — SQLAlchemy Session bị dùng qua thread trong live pipeline

- Bằng chứng: `tf_api/api/routes_live.py:312-320` tạo `db_session` trong request/event-loop thread; `:332-336` truyền adapter chứa session đó vào `StorageWorker`, worker xử lý ở daemon thread.
- SQLAlchemy `Session` không phải object dùng đồng thời giữa các thread; `check_same_thread=False` trong SQLite không biến session thành thread-safe.
- Impact: race, transaction state sai, lỗi session ngẫu nhiên hoặc mất dữ liệu khi nhiều camera/stream hoạt động.
- Cải thiện: tạo session/adapter bên trong worker thread qua `session_factory`; commit/rollback theo batch; đóng session chắc chắn khi stream stop; test concurrency nhiều camera với PostgreSQL.
- Tiêu chí đạt: không có session object vượt thread boundary; stress test không có cross-thread/session errors.

### P1-12 — StorageWorker có thể chết khi DB ném exception ngoài danh sách catch

- Bằng chứng: `tf_worker/storage/storage_worker.py:261-280` chỉ catch `(OSError, KeyError, ValueError)`; insert và aggregate ở `:341-365`/`:409-420` cũng không bắt các exception DB phổ biến như `SQLAlchemyError`.
- `:272-273` chỉ log khi batch commit fail nhưng tiếp tục với transaction state có thể đã lỗi.
- Impact: một lỗi DB/constraint có thể làm daemon storage thread dừng, pipeline tiếp tục nhưng event không được persist; không có automatic restart/health state.
- Cải thiện: catch phân loại `SQLAlchemyError`, rollback transaction, dead-letter có giới hạn và retry/backoff; watchdog phát hiện worker chết; expose queue/error/dead-letter metrics; không nuốt lỗi commit.
- Tiêu chí đạt: DB outage/constraint failure không làm thread chết âm thầm; recovery test chứng minh event sau outage được xử lý hoặc nằm trong DLQ có thể replay.

### P1-13 — Production defaults và TLS boundary chưa an toàn

- `deploy/stack/.env.example` đặt `APP_ENV=production` cùng placeholder JWT/database password; code chỉ reject đúng dev fallback, chưa reject placeholder/entropy yếu.
- Compose có fallback password mặc định và Nginx stack chỉ expose HTTP port 80 (`deploy/stack/docker-compose.yml:230-233`); `deploy/proxy/nginx.conf:8-10` ghi TLS do proxy khác terminate nhưng không có guard bắt buộc.
- Impact: thao tác triển khai nhầm có thể dùng secret public; nếu expose trực tiếp, credential/token truyền qua HTTP.
- Cải thiện: production preflight fail khi còn placeholder/weak secret; secret injection từ secret manager; bật HTTPS/HSTS ở edge hoặc fail deploy nếu không có trusted TLS proxy; CORS production phải explicit.
- Tiêu chí đạt: deploy không start khi secret placeholder; có TLS termination được kiểm chứng và không có bearer token qua plaintext.

## 4. Findings P2 — cần xử lý trước khi chấp nhận risk để ship

### P2-01 — Frontend có nhiều sink `innerHTML` với dữ liệu API chưa escape

- Bằng chứng: `frontend/js/pages/cameras.js:4-32`, `frontend/js/pages/alerts.js`, `frontend/js/pages/events.js`, `frontend/js/pages/live.js`, `frontend/js/pages/models.js`, `frontend/js/pages/users.js`, `frontend/js/pages/jobs.js`, `frontend/js/pages/health.js`; `frontend/js/core.js:257-276` dựng option bằng template string.
- Các field như camera name/source, alert message, model description, username, event/lane ID được chèn trực tiếp; một số camera ID còn được đưa vào inline `onclick`.
- Impact: dữ liệu cấu hình/API bị kiểm soát có thể trở thành stored XSS hoặc phá markup/handler.
- Cải thiện: dùng DOM API + `textContent`, escape helper tập trung, bỏ inline handler; CSP nonce/hash không dùng `unsafe-inline`; thêm DOM XSS tests.

### P2-02 — Frontend phụ thuộc CDN mutable và CSP quá rộng

- `frontend/index.html:8-14` tải Tailwind, Lucide, ApexCharts và Google Fonts từ CDN, có package `latest`/không pin SRI.
- `deploy/proxy/nginx.conf:32` cho phép `unsafe-inline` và nhiều script/style origin.
- Impact: supply-chain/CDN compromise hoặc thay đổi version làm thay đổi runtime; CSP giảm đáng kể khả năng chặn XSS.
- Cải thiện: bundle/pin asset, SRI hoặc self-host; bỏ `unsafe-inline`; CSP theo nonce/hash; lock file cho frontend nếu dùng toolchain.

### P2-03 — SPA không có login flow

- `frontend/pages/` không có login page; `frontend/js/core.js:191-205` chỉ đọc token từ `localStorage`, còn API backend yêu cầu JWT.
- Impact: user mới không có đường đăng nhập từ UI; phải tự seed token hoặc gọi API ngoài UI, khiến workflow chính không hoàn chỉnh.
- Cải thiện: thêm login/refresh/expired-session screen, role-aware navigation, redirect khi 401, không lưu bearer token dài hạn trong localStorage nếu có thể dùng HttpOnly cookie.

### P2-04 — Test suite chưa bao phủ critical path và integration đang treo

- Chỉ có 4 file test, 15 test được collect; không có `tests/e2e`, `tests/security`, `tests/performance`, `tests/resilience`, `tests/model_serving` hoặc migration test.
- `tests/conftest.py:126-136` tạo app test rút gọn, không include jobs/admin/ws/live/detect/users/audit; vì vậy route auth bypass và Docker import failure không bị bắt.
- Integration đầu tiên timeout ở request anonymous, nhưng CI hiện vẫn chạy `pytest tests -q` không có timeout/diagnostic (`.github/workflows/ci.yml:24-25`).
- Cải thiện: tách fast unit khỏi integration; thêm timeout; dùng `pytest-timeout` hoặc watchdog CI; test full `tf_api.main`; contract matrix; browser smoke; auth negative paths; fixture cleanup.

### P2-05 — Ruff/mypy gate không phản ánh chất lượng runtime

- Ruff runtime mới fail 227 lỗi; full repo fail 412 lỗi. Nhiều lỗi trong `legacy/` làm nhiễu nhưng active code cũng có import ordering, unused import, broad/pass exception, thread/error handling.
- CI chỉ chạy `ruff check tests --select I,F` (`.github/workflows/ci.yml:21-22`), không lint `tf_*`, `scripts`, deploy config hoặc frontend.
- Mypy fail trước khi type-check vì duplicate module `monitoring`; cần `--explicit-package-bases`/package layout hoặc exclude chính thức.
- Cải thiện: gate active code trước, budget lint theo PR, sửa duplicate package identity, bật type-check các boundary và không để `legacy` che lỗi runtime.

### P2-06 — Không có dependency/container provenance

- `requirements.txt` phần lớn không pin version; `deploy/api/Dockerfile`, `deploy/worker/Dockerfile` dùng `python:3.10-slim`; Compose dùng `redis:7-alpine`, `postgres:16-alpine`, `nginx:1.27-alpine` không có digest.
- Không có SBOM, pip audit, image scan, signing/provenance hoặc artifact upload trong CI.
- Cải thiện: lock dependencies, pin base image digest, build multi-stage tối thiểu, scan CVE/license, generate SBOM, attach commit/image digest vào release evidence.

### P2-07 — Model upload/registry chưa có quota, integrity và path policy đầy đủ

- `tf_api/api/routes_models.py:85-121` copy upload stream không có max bytes/quota/hash/quarantine/scan; `class_mode` không validate ở endpoint.
- `:140-146` cho phép admin set `model_path` tùy ý; `:172-178` `remove_file` unlink path từ registry.
- Impact: disk exhaustion hoặc registry trỏ tới file ngoài weight root; model artifact không có provenance/hash.
- Cải thiện: giới hạn kích thước/content, validate class mode/path dưới `weights/`, atomic registry write, hash/signature, model load sandbox/health test và audit đầy đủ.

### P2-08 — Health endpoint là liveness giả, chưa có readiness/SLO alert

- `tf_api/services/health_checker.py:131-145` luôn trả `"status": "ok"` dù dependency unhealthy; public `/api/health` tại `tf_api/api/routes_health.py:9-11` chỉ gọi `public_health()` trả `{"status":"ok"}`.
- Không có `/readyz`, không có alert rule/dashboard/runbook trong `deploy/` hoặc `.github/`; `health/worker` (`routes_health.py:14-20`) luôn trả `alive: false` như placeholder.
- Cải thiện: tách liveness/readiness; readiness kiểm tra DB schema, Redis và model khi required; SLO cho p95/error/queue/model; alert test; không expose platform details ngoài policy.

### P2-09 — Retention policy chỉ tồn tại dưới dạng class, chưa được schedule

- `tf_worker/storage/retention.py` định nghĩa `RetentionCleaner`, nhưng không có call site `RetentionCleaner(...)`, cron, scheduler hoặc worker loop.
- Settings có `data_retention_days` nhưng không nối vào cleanup thực tế.
- Impact: crop/event/metrics tăng không giới hạn, rủi ro chi phí và privacy; retention setting tạo cảm giác đã hoạt động nhưng không có hiệu lực.
- Cải thiện: job retention idempotent có lock/leader election, dry-run, metrics, audit, DB/file consistency và test boundary timezone.

### P2-10 — Query/performance scaling còn N+1 và thiếu index/metric discipline

- `routes_dashboard.py:49-64` query nhiều lần cho từng camera.
- `SqlMetricsRepository.get_avg_fps/get_avg_latency()` (`tf_db/repositories.py:497-525`) tải rows về Python thay vì `AVG()` SQL; `RuntimeMetric` không có index composite theo camera/time.
- Prometheus labels chứa `camera_id`, `lane_id`, `direction` (`tf_common/monitoring/metrics.py:40-67`), có thể cardinality tăng theo cấu hình camera.
- Cải thiện: aggregate query theo batch, SQL `AVG`, composite indexes, bounded camera/lane labels hoặc metric allow-list; benchmark với volume đại diện và SLO p95/p99.

### P2-11 — Config/state writes chưa nhất quán về atomicity và locking

- Lanes/zones có atomic helper, nhưng settings `tf_api/api/routes_settings.py:77-80`, models `routes_models.py:34-37`, camera config `routes_cameras.py:88-91`, live reload `routes_live.py:781-786` dùng plain write.
- Impact: hai admin request đồng thời có thể mất update hoặc để YAML/JSON dở dang khi process chết.
- Cải thiện: schema validate trước write, temp file + fsync + replace, file lock, revision/version hoặc DB-backed config; test concurrent update/crash recovery.

### P2-12 — XML parser fail-open về parser không an toàn

- `tf_worker/evaluation/detrac_xml_parser.py:4-8` fallback sang stdlib `xml.etree.ElementTree` nếu `defusedxml` thiếu; Ruff cảnh báo `S314` tại `:45`.
- Cải thiện: dependency bắt buộc cho đường parse untrusted, fail-fast nếu thiếu `defusedxml`, giới hạn file size/depth và thêm malicious XML tests.

### P2-13 — Camera/user authorization chưa có phạm vi resource

- Các route chỉ phân biệt admin/operator/viewer, không có tenant/camera ACL; mọi authenticated user có thể list camera/jobs/events và truy vấn camera ID bất kỳ.
- Nếu sản phẩm phục vụ nhiều nhóm/khách hàng, đây là thiếu isolation; hiện chưa có tenant model/field/filter trong `tf_db/models.py`.
- Cải thiện: quyết định rõ single-tenant hay multi-tenant. Nếu multi-tenant, thêm tenant ownership, policy dependency và DB-level filtering; test tenant A/B ở API, cache, WebSocket, exports và backups.

## 5. Findings P3 / hardening và tài liệu

- `docs/REALTIME_OPTIMIZATION_AUDIT_2026-07-09.md:109-116` ghi “test suite active pass”, nhưng audit hiện tại chứng kiến integration test treo; cần cập nhật docs theo evidence mới.
- `AGENTS.md:46-51` gọi `pytest tests -q` là quick check nhưng không có timeout và hiện không hoàn thành; nên ghi rõ fast gate/integration gate.
- `AGENTS.md`/README cần ghi login flow, API key policy, required `deploy/stack/.env`, model artifact provisioning và cách chạy worker độc lập.
- Các file `.yaml.lock` trong `configs/` cần policy rõ: generated lock có commit hay không, ai tạo và có được copy vào image không.
- Cần thêm accessibility/browser matrix: keyboard navigation, labels, focus, mobile layout, error/loading/empty states và console error budget.
- `tf_common/circuit_breaker.py` dùng state mutable không có lock; cần test đa thread và điều chỉnh half-open behavior.
- Nên tách lint legacy khỏi active runtime bằng config `exclude`, nhưng không dùng exclude để che lỗi trong module active.
- Git repository metadata đang hỏng: `git show-ref` báo `bad ref HEAD` và `git fsck --full` báo invalid pointers/missing blobs. Cần repair/re-clone hoặc khôi phục refs trước khi dùng commit SHA, branch promotion, diff review và release provenance.

## 6. Thứ tự triển khai đề xuất

### Phase 0 — chặn lỗi release và khôi phục khả năng kiểm chứng

1. Repair/re-clone Git metadata; xác định commit baseline sạch.
2. Sửa API image dependency boundary; build image sạch.
3. Sửa model path/default và provision artifact có hash.
4. Bắt auth cho dashboard/detect, hoàn thiện JWT revoke/type/active-user và loại query token.
5. Sửa Redis env cho WebSocket, session-per-worker-thread và storage worker recovery.
6. Sửa auto-ingest transaction/idempotency và job state persistence.

### Phase 1 — data/release safety

1. Tạo Alembic baseline/revisions, migration gate và backup-restore test.
2. Tạo `/livez`/`/readyz`, metrics/SLO/alerts/runbook.
3. Thêm test app đầy đủ và integration timeout; làm cho full pytest kết thúc deterministic.
4. Chốt production secrets/TLS/CORS policy; pin dependencies/base images và thêm SBOM/CVE scan.

### Phase 2 — chất lượng và scale

1. Xử lý XSS/DOM sink, bỏ CDN mutable hoặc pin/SRI, siết CSP.
2. Thêm login UI, role-aware navigation và session handling.
3. Tối ưu dashboard/query/index/Prometheus cardinality.
4. Kích hoạt retention scheduler và kiểm thử cleanup/restore.
5. Quyết định single-tenant/multi-tenant rồi triển khai resource isolation nếu cần.

## 7. Definition of Done cho lần audit lại

- `docker compose ... config`, build API/worker/frontend và smoke full stack pass trong môi trường sạch.
- API import không phụ thuộc package ngoài manifest; worker và API có boundary rõ.
- Anonymous request không đọc được dashboard/detect; role/resource matrix có test deny path.
- Logout/disable/role-change làm token cũ vô hiệu theo policy; không có token trong URL/log.
- Job/event/aggregate survives restart, retry và DB outage; auto-ingest có commit, rollback, idempotency.
- Có Alembic revision, migration log, backup-restore evidence và schema compatibility check.
- Model registry chỉ chứa artifact tồn tại, đúng hash, đúng class mode; default worker chạy được.
- `pytest` full suite kết thúc trong timeout; Ruff active code pass; mypy có module layout hợp lệ.
- Có E2E auth/main workflow, security smoke, migration/restore, performance smoke, resilience và model golden tests.
- Release report trace được commit/image/environment; mọi P0/P1 đã đóng hoặc release bị block.

## 8. Follow-up: thay đổi đã thực hiện và bằng chứng mới nhất

### Đã sửa trong working tree

1. `start.sh` kiểm tra Alembic trước khi bootstrap; dependency FastAPI/Uvicorn/HTTPX/AnyIO và Alembic được pin để giảm runtime drift.
2. Alembic baseline `0001_initial` được thêm; startup chạy migration qua CLI subprocess để không treo Uvicorn lifespan. API Docker image đã copy `alembic.ini`, `alembic/` và cài `tf_worker`.
3. Default model được chuẩn hóa về `weights/yolo11n.pt`; registry chỉ còn artifact tồn tại và worker/API dùng cùng default.
4. Job state được persist vào `inference_jobs`; auto-ingest commit/rollback rõ ràng, aggregate buckets được ghi, lane-change CSV được ingest; model ID không tồn tại bị từ chối.
5. Dashboard, detect, live, models, users, settings và WebSocket đã có auth/RBAC tương ứng; JWT kiểm tra type, user active, token version; logout/role/password change revoke token.
6. MJPEG không còn nhận access token dài hạn trên query string; dùng one-use short-lived stream ticket. Snapshot/export frontend đã gửi JWT.
7. Redis WebSocket đọc `REDIS_HOST`/`REDIS_PORT`, kiểm tra Origin/subscription; live storage tạo SQLAlchemy session trong worker thread. Worker container được nối DB adapter để persist event.
8. `/detect/frame` được sửa thành multipart `config` JSON + `image` upload; model path bị giới hạn dưới `weights/` và file phải tồn tại.
9. Lane-change storage/API đọc đúng `lane_change_events`; StorageWorker và pipeline đã persist/publish true lane-change event.
10. Settings write dùng temp file + replace; frontend có `escapeHtml()` và các màn hình chính đã escape dữ liệu API trước khi render.
11. Ruff đã pass toàn repo sau khi loại `legacy/` khỏi runtime lint và ghi rõ test-only exceptions; frontend JS `node --check` pass.
12. Docker API/worker đã build thành công với PyTorch CPU `2.7.1+cpu`; import smoke trong cả hai image pass. Compose giữ đường cài PyTorch CUDA riêng cho worker-GPU.

### Verification mới nhất

| Gate | Kết quả |
|---|---|
| `python -m compileall ...` | PASS |
| `node --check` toàn bộ `frontend/js/*.js` | PASS |
| `ruff check .` | PASS |
| Unit/realtime/query tests | PASS — 8 tests |
| Fresh SQLite `alembic upgrade head` | PASS |
| `npm run dev` với SQLite tạm + bootstrap admin | PASS — Uvicorn báo `Application startup complete` |
| Re-run đúng `npm run dev` sau các thay đổi | PASS — migration, bootstrap admin, metrics và Uvicorn startup đều hoàn tất; `timeout` kết thúc process chủ động với mã 124 |
| Auth login/decode/logout-revocation smoke | PASS |
| Seed DB smoke | PASS |
| OpenAPI route smoke | PASS — 49 paths; `/detect/frame` là multipart |
| `docker compose ... config --quiet` | PASS |
| Docker API/worker build | PASS — import smoke pass; image dùng PyTorch `2.7.1+cpu` |
| `pytest tests/unit -q` | PASS — 8 tests |
| `pytest tests -q` | BLOCKED — integration `TestClient` treo; lệnh bị giới hạn 90 giây trong sandbox |
| Integration tests qua Starlette TestClient | BLOCKED — AnyIO/TestClient treo trong sandbox trước request; đã thêm timeout CI |
| Docker Compose full stack runtime | BLOCKED — chưa có Postgres/Redis runtime; browser/frontend E2E chưa chạy |

### Còn block trước khi tuyên bố production-ready

- Chạy integration contract matrix bằng HTTP server thật ở môi trường cho phép socket và dependency sạch; không dùng kết quả TestClient timeout làm PASS.
- Smoke API/worker/frontend/nginx với Postgres + Redis thật; kiểm tra worker persist event và WebSocket cross-container. API/worker image build đã pass, nhưng đây chưa phải full-stack runtime smoke.
- Chạy browser E2E login, dashboard, live, lane/zone editor, model upload, job và CSV export.
- Chạy security scan/DAST, dependency/image scan, migration backup-restore và load/resilience test với camera/weights thực.

Vì các gate trên chưa có evidence, kết luận hiện tại vẫn là **NO-GO**; các lỗi còn lại không thể trung thực gọi là “không còn bất kỳ lỗi nào” chỉ từ compile/unit smoke.

## 10. Audit tiếp tục và kết quả sửa lỗi (2026-07-10)

Phần này là kết quả kiểm tra lại trên working tree hiện tại; các nhận định lịch sử phía trên không được dùng thay cho bằng chứng mới.

### Lỗi đã tái hiện và đã sửa

| ID | Mức | Bằng chứng tái hiện | Sửa |
|---|---|---|---|
| RT-01 | P1 | `trafficflow_api` restart loop; `passlib` lỗi với `bcrypt 5.x` (`bcrypt` không có `__about__`, password error) | Pin `bcrypt>=4.0,<4.1` trong package/requirements; Docker image sau sửa import được và dùng `bcrypt 4.0.1` |
| RT-02 | P1 | Nginx restart loop: `proxy_pass cannot have URI part in location given by regular expression` ở dòng 107 | Regex route `/api/infer/video` dùng `proxy_pass http://api_backend` không kèm URI |
| RT-03 | P1 | Browser gateway không truy cập được vì `API_BASE_URL=http://api:8000` là hostname chỉ tồn tại trong Docker network | Frontend/Compose dùng `/`, tất cả API request đi qua nginx cùng origin |
| RT-04 | P1 | Worker crash vì bind-mounted `outputs`/`storage`/`logs` không writable với user `app` | Stack dùng named volumes; image tạo sẵn thư mục và chown cho user runtime |
| RT-05 | P1 | Worker load camera nhưng mất `detector`/`tracking`, sau đó lỗi `Missing 'tracking' section` | Worker dùng `compile_camera_config`, overlay đầy đủ pipeline sections thay vì chỉ copy input/output |
| RT-06 | P1 | Camera configs có lane list rỗng, worker lỗi `'lanes' must be a non-empty list` | Cho phép camera chưa cấu hình lane chạy detection/tracking; counting không phát sinh lane event |
| RT-07 | P1 | Ultralytics cố pip-install `lap` lúc runtime và thất bại vì user/container path | Khai báo `lap>=0.5.12` lúc build; đặt HOME/Ultralytics/Matplotlib cache vào `/tmp` |
| RT-08 | P1 | `/api/admin/metrics` bị SPA catch-all trả 404 | Đưa route metrics trước catch-all và thêm unit regression test |
| RT-09 | P2 | `/api/health/worker` luôn trả `alive:false`, không phản ánh worker thật | Worker publish heartbeat Redis TTL 30s; API kiểm tra heartbeat và trả camera count |
| RT-10 | P1 | Chạy đúng `npm run dev` khi port 8000 đã bị tiến trình khác chiếm làm Uvicorn thoát với `address already in use` | Development tự chọn port trống tiếp theo (đã xác minh chọn 8002); `PORT=...` explicit vẫn giữ lỗi rõ ràng |
| RT-11 | P2 | Worker config compiled từ camera schema không tự lấy `camera_id`/`job_id` từ `server`, khiến direct/offline execution ghi vào `outputs/unknown` | `_build_pipeline_config` giữ server metadata và fallback camera id từ compiled server |

### Bằng chứng sau sửa

- Local: `bash -n start.sh` PASS; `compileall` PASS; `node --check` toàn bộ frontend PASS; `ruff check .` PASS; `pytest tests/unit -q` = **9 passed**; pytest collection = **19 tests**.
- API Docker image: import app PASS, `bcrypt 4.0.1`.
- Compose với Postgres 16 + Redis 7: API healthy, migration chạy, bootstrap admin chạy; nginx route `/api/health`, `/api/readyz`, `/`, `/config.js` trả 200; login, cameras, models, jobs, dashboard và admin metrics đều trả 200 khi có token.
- `config.js` qua nginx trả `API_BASE_URL: "/"`, không còn `http://api:8000` trong browser.
- Worker sau sửa đã load cả 3 camera, khởi tạo pipeline và xử lý frame từ các nguồn YouTube; không còn log `PermissionError`, thiếu section cấu hình hoặc lỗi cài `lap` trong lần smoke mới nhất. Nguồn YouTube là external runtime nên không được coi là holdout kiểm thử offline.
- Cross-container worker health: `/api/health/worker` trả `{"alive":true,"camera_count":3}` qua nginx; `/api/readyz` trả `ready`.
- Exact command smoke with port 8000 occupied: `npm run dev` tự chuyển sang port 8002, startup hoàn tất và `/api/readyz` trả 200.
- Offline worker smoke với 3 synthetic frames: pipeline xử lý hết 3 frame và tạo `frames.jsonl`, `counts.csv`, `occupancy.csv`, `counts_summary.csv`, `lane_changes.csv`.

### Các vấn đề còn mở / release gate

1. Full `pytest tests` hiện đã chạy **20 passed** sau khi bổ sung regression test worker config. Local venv vẫn chưa có `pytest-timeout`, nhưng lần chạy có shell timeout 60 giây và hoàn tất trong 17 giây; CI vẫn cần cài đúng `requirements-dev.txt` để dùng gate `--timeout=30`.
2. `mypy` còn 120 lỗi typing ở 32 file; CI hiện chưa chạy mypy.
3. Chưa có browser Playwright E2E, migration backup/restore, DAST/dependency scan, load/resilience evidence.
4. Worker mặc định dùng 3 YouTube live URLs; chưa có camera fixture offline để chứng minh end-to-end counting/persist/WebSocket một cách deterministic.
5. Git object database đang hỏng (`fatal: bad object HEAD`), nên chưa thể xác nhận provenance/diff bằng Git.

**Release decision: GO-WITH-RISK cho local/runtime workflow.** Các luồng npm dev, browser asset loading, API auth/data, Postgres/Redis/nginx, worker live heartbeat và worker offline đã có evidence PASS. Các gate production còn thiếu (mypy, backup/restore, security scan, Git provenance) vẫn được giữ trong risk register và chưa được coi là lỗi runtime đã sửa.

## 11. Live Monitoring — audit và sửa lỗi (2026-07-10)

### Lỗi tái hiện

| ID | Mức | Lỗi | Sửa |
|---|---|---|---|
| LIVE-01 | P1 | Nginx chỉ proxy `/api/` và `/ws/`; các request `/live/{camera}/stream-ticket`, `/stream.mjpg`, `/metrics` rơi vào frontend SPA | Thêm `location /live/` proxy vào `api_backend` với buffering off và timeout dài |
| LIVE-02 | P1 | Redis live payload occupancy không có envelope `type/camera_id/data`, frontend không xử lý được; occupancy rỗng không được publish nên UI bị stale | Chuẩn hóa `occupancy_update`, publish cả trạng thái rỗng/changed/heartbeat |
| LIVE-03 | P1 | `count_event` và `lane_change_event` chỉ dùng in-process bus; Docker worker/API khác process nên WebSocket không nhận | Publish các event vào Redis channel `traffic:live:{camera_id}` đồng thời giữ fallback in-process |
| LIVE-04 | P2 | Lane-change payload dùng `previous_stable_lane/current_stable_lane`, frontend chờ `previous_lane_id/current_lane_id` | Bổ sung hai field frontend contract vào payload |
| LIVE-05 | P1 | Client đóng WebSocket có thể làm Redis listener văng `WebSocketDisconnect/RuntimeError`, ảnh hưởng các client khác | Bắt disconnect trong receive/broadcast, loại client lỗi khỏi subscriber mà không dừng listener |

### Evidence sau sửa

- Qua nginx: `/live/YT_LIVE_TEST/stream-ticket` trả 200 có token; `/live/YT_LIVE_TEST/metrics` trả 200 có metrics schema.
- MJPEG qua nginx trả `200`, `Content-Type: multipart/x-mixed-replace; boundary=frame`, nhận được khoảng 4 MB frame data trong 12 giây.
- WebSocket `/ws/live` authenticate thành công và nhận `connected`.
- Redis → API WebSocket: publish synthetic `count_event` vào `traffic:live:YT_LIVE_TEST`, browser client nhận đúng event.
- Worker live → Redis → WebSocket: client nhận `occupancy_update` và `lane_change_event` từ camera đang chạy; lane event có `previous_lane_id/current_lane_id`.
- Sau khi một client disconnect, client tiếp theo vẫn nhận Redis event; log không còn lỗi listener disconnect.

## 12. Log npm và trạng thái hiển thị pipeline (2026-07-10)

### Lỗi tái hiện

| ID | Mức | Bằng chứng | Sửa |
|---|---|---|---|
| NPM-01 | P1 | Log `npm run dev` ghi `ImportError: numpy.core.multiarray failed to import` do venv dùng `--system-site-packages`, trộn ABI NumPy/OpenCV của hệ thống | Bootstrap dùng venv cô lập, không dùng system site packages |
| NPM-02 | P1 | Bootstrap mặc định kéo Torch CUDA rất lớn rồi fail CA bundle; local không cần GPU | Cài Torch/Torchvision CPU wheel cố định; dùng CA bundle `/etc/ssl/certs/ca-certificates.crt` |
| LIVE-06 | P1 | Capture thread crash `UnboundLocalError: record_stream_state referenced before assignment`; metrics giữ `process_fps=0`, UI hiện “Pipeline starting...” liên tục | Loại import local gây shadowing, thêm lifecycle state và error message từ pipeline |

### Evidence sau sửa

- Fresh isolated npm bootstrap đã cài CPU Torch, khởi động API và `/api/readyz` trả 200; không còn `multiarray`/traceback trong log startup.
- Live metrics sau MJPEG request: `process_fps=12.4`, `source_fps=30.0`, `status=active`, nhận 4.2 MB frame.
- UI hiện phân biệt `Starting pipeline...`, `Connecting to camera...`, `Reconnecting camera...`, `Pipeline error`, `No frames received`, không dùng “Pipeline starting...” để che mọi lỗi.
- Docker API dependency build sau khi loại package trực tiếp không dùng: PASS.

## 13. Live pipeline PermissionError (2026-07-10)

- **Lỗi:** `Pipeline start failed: PermissionError` khi local API tạo `storage/YT_LIVE_TEST`.
- **Nguyên nhân:** thư mục repo `storage/`, `outputs/`, `logs/` do Docker tạo với `root:root` và mode `755`; user chạy `npm run dev` không có quyền ghi.
- **Sửa:** local `start.sh` dùng `STORAGE_ROOT=data/storage`, `OUTPUT_DIR=data/outputs`; Docker Compose dùng named `/app/storage` và `/app/outputs`. Job API cũng đọc `OUTPUT_DIR` từ environment thay vì hard-code.
- **Evidence:** `data/storage` và `data/outputs` writable; Docker API rebuild/start healthy với `STORAGE_ROOT=/app/storage`, `/api/readyz` trả 200 và không còn permission error log. Fresh local GPU `npm run dev` cũng tạo stream thành công.

### GPU local smoke sau PermissionError fix

- GPU: Quadro T2000, CUDA 12.1, Torch `2.2.2+cu121`.
- Local `npm run dev` fresh bootstrap: migration, login và `/api/readyz` PASS.
- Live MJPEG: HTTP 200, nhận 13.7 MB dữ liệu frame.
- Metrics: `status=active`, `process_fps=22.4`, `source_fps=30.0`, `output_fps=5.8`, `gpu_available=true`, `gpu_util_pct=1.1`.
- Log không có `PermissionError`, `Traceback` hoặc `Application startup failed`; shutdown dọn `StorageWorker` và live stream đúng.
