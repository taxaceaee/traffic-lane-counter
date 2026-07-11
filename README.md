# TrafficFlow

Vision-based real-time per-lane vehicle counting.

## Repo layout (deploy-oriented)

```
packages/                 Shared Python code (import: tf_api, tf_worker, …)
services/
  backend/                API image + docs
  frontend/               SPA (static) + image
  model/                  Inference worker image + docs
  database/               Alembic migrations
deploy/
  docker-compose.yml      Full stack
  nginx.conf              Reverse proxy
configs/                  Cameras, lanes, zones, models
weights/                  YOLO weights
scripts/                  Ops helpers
tests/
```

Each `services/*` folder is a **deploy unit** you can ship to a different host
(see `services/*/README.md` and free-host notes there).

## Quick start (local)

```bash
./start.sh
# API + SPA: http://localhost:8000  (or next free port)
# Login (dev): admin / admin123
```

```bash
# Frontend only (if API already running)
API_BASE_URL=http://localhost:8000 .venv/bin/python -m scripts.serve_frontend
```

```bash
# Full Docker stack
docker compose -f deploy/docker-compose.yml up --build
```

## Multi-server deploy

| Piece | Build | Typical free host |
|---|---|---|
| Frontend | `services/frontend` | Cloudflare Pages / Vercel |
| Backend | `services/backend/Dockerfile` | Render / Oracle Free VM |
| Model | `services/model/Dockerfile` | Oracle Free VM / GPU host |
| Database | managed Postgres + `services/database` migrations | Supabase / Neon |
| Redis | managed | Upstash |

```bash
# Example: build images separately
docker build -f services/backend/Dockerfile -t trafficflow-api .
docker build -f services/frontend/Dockerfile -t trafficflow-web .
docker build -f services/model/Dockerfile -t trafficflow-model .
```

Set `SERVE_FRONTEND=false` on the API when the SPA is hosted elsewhere, and
point `CORS_ORIGINS` / frontend `API_BASE_URL` at the public API.

## Offline pipeline

```bash
python3 -m scripts.run_occupancy \
  --config configs/cameras/YT_LIVE_TEST.yaml \
  --source /path/to/video.mp4
```

## Dev checks

```bash
export PYTHONPATH=packages
.venv/bin/python -m compileall packages scripts
.venv/bin/python -m ruff check packages scripts tests
.venv/bin/python -m pytest tests -q
```

## License

MIT
