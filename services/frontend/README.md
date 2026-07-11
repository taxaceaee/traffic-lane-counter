# Frontend (SPA)

Static dashboard (`index.html` + `js/` + `pages/`). No build step.

## Local

```bash
API_BASE_URL=http://localhost:8000 .venv/bin/python -m scripts.serve_frontend
# → http://localhost:3000
```

## Docker

```bash
docker build -f services/frontend/Dockerfile -t trafficflow-frontend .
docker run --rm -p 3000:3000 \
  -e API_BASE_URL=https://api.example.com \
  trafficflow-frontend
```

## Deploy free

Cloudflare Pages / Vercel / Netlify: upload this folder (or point static root here).  
Set runtime config so `config.js` has your API URL (or inject at build/CDN).

## Depends on

- **backend** public URL (`API_BASE_URL`)
