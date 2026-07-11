# Repo Layout

## Active runtime

- `tf_api/` — backend API process
- `tf_worker/` — worker process (pipeline, storage, video IO)
- `tf_db/` — database models and repositories
- `tf_core/` — CV/AI domain logic
- `tf_common/` — shared utilities
- `frontend/` — SPA
- `configs/` — cameras, zones, lanes, models, settings
- `scripts/` — serve frontend, seed DB, import JSONL, offline pipeline
- `deploy/` — api / frontend / worker / proxy / stack
- `tests/` — unit + integration
- `weights/` — model weights (gitignored binaries; `.gitkeep` tracked)
- `alembic/` — DB migrations

## Not in runtime

- Local data: `data/`, `outputs/`, `logs/`, `storage/` (gitignored)
- Generated reports: `test-reports/` (gitignored)
- DETRAC / offline benchmark suite was removed; use live pipeline + tests instead.
