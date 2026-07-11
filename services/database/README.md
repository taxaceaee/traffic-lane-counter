# Database

Postgres (production) or SQLite (local dev). Schema via **Alembic**.

## Layout

```
services/database/
  alembic.ini
  alembic/           # migrations
  README.md
```

ORM models live in shared package: `packages/tf_db/`.

## Local (SQLite)

```bash
export DATABASE_URL=sqlite:///./data/trafficflow.db
# migrations run automatically on API start
.venv/bin/alembic -c services/database/alembic.ini upgrade head
```

## Managed free Postgres

Set `DATABASE_URL` to Supabase / Neon / Railway, then run migrations once:

```bash
DATABASE_URL=postgresql://... \
  .venv/bin/alembic -c services/database/alembic.ini upgrade head
```

## Docker (with full stack)

Postgres is defined in `deploy/docker-compose.yml` service `postgres`.  
API applies migrations on boot.

## Redis

Not in this folder — use Upstash free or compose `redis` service for pub/sub.
