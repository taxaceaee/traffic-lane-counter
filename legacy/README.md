# Legacy Code

This directory contains superseded implementations that are no longer part of
the primary deployment path:

- `backend/` - older backend/runtime tree duplicated by `tf_api`, `tf_core`, and `tf_worker`
- `shared/` - older shared pipeline package replaced by `tf_core` and `tf_common`
- `detection_server/` - earlier standalone detection service
- `frontend_streamlit/` - Streamlit iframe wrapper that used to host the SPA
- `code/` - one-off analysis scripts and notebooks
- `tools/` - one-off codemods and migration helpers
- `tools/refactor_frontend.py` - one-time frontend extraction helper
- `docs/` - archived planning notes that no longer match the live runtime

Nothing under `legacy/` is required for the current API, worker, frontend, or
database deployment flow.
