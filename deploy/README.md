# Deploy

## Full stack (one host)

```bash
cp deploy/.env.example deploy/.env   # optional
docker compose -f deploy/docker-compose.yml up --build
```

## Per-service images (multi-server)

| Service | Build | Notes |
|---|---|---|
| **Backend** | `docker build -f services/backend/Dockerfile -t trafficflow-api .` | Needs Postgres + Redis |
| **Frontend** | `docker build -f services/frontend/Dockerfile -t trafficflow-web .` | Set `API_BASE_URL` |
| **Model** | `docker build -f services/model/Dockerfile -t trafficflow-model .` | Needs weights + GPU/CPU |
| **Database** | Use managed Postgres / compose `postgres` | Migrate: `alembic -c services/database/alembic.ini upgrade head` |

See `services/*/README.md` for env vars and free-host tips.

## Proxy

`deploy/nginx.conf` routes `/` → frontend, `/api` + `/ws` + `/live` → backend.
