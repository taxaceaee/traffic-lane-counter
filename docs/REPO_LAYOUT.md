# Repo Layout

## Active runtime

- `tf_api/` - backend API process
- `tf_worker/` - worker process
- `tf_db/` - DB layer
- `tf_core/` - CV/AI domain logic
- `tf_common/` - shared utilities between services
- `frontend/` - deployable SPA
- `configs/` - runtime config files
- `deploy/api/` - backend container build
- `deploy/frontend/` - frontend container build
- `deploy/worker/` - worker container build
- `deploy/proxy/` - reverse proxy config
- `deploy/stack/` - full local integration stack

## Documentation policy

Keep only curated docs that match the live runtime. Remove stale generated docs
instead of leaving contradictory snapshots in the repo.
Historical audits and migration notes should be kept only when they explain a
current runtime decision; otherwise remove them instead of creating another
archive tree.
