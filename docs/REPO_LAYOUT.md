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

## Archived runtime

- `legacy/backend/`
- `legacy/shared/`
- `legacy/detection_server/`
- `legacy/frontend_streamlit/`
- `legacy/code/`
- `legacy/docs/`

These archived trees remain for reference only and should not be used for new
deployment work.

## Documentation policy

Keep only curated docs that match the live runtime. Remove stale generated docs
instead of leaving contradictory snapshots in the repo.
Historical audits, migration notes, and superseded planning docs belong in
`legacy/docs/`.
