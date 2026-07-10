# Deploy Layout

- `api/` - backend image build
- `frontend/` - standalone SPA image build
- `worker/` - inference worker image build
- `proxy/` - optional reverse proxy
- `stack/` - local multi-service compose stack

Use these folders independently when splitting deployment across different
machines or platforms.
