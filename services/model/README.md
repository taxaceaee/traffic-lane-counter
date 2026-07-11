# Model (Inference Worker)

YOLO + ByteTrack + lane counting. Long-running process; prefers GPU.

## Local

```bash
.venv/bin/python -m tf_worker.worker
```

## Docker (CPU)

```bash
docker build -f services/model/Dockerfile -t trafficflow-model .
docker run --rm \
  -e DATABASE_URL=postgresql://... \
  -e REDIS_HOST=... \
  -e API_BASE_URL=http://api:8000 \
  -v "$(pwd)/weights:/app/weights:ro" \
  -v "$(pwd)/configs:/app/configs:ro" \
  trafficflow-model
```

## Docker (GPU)

```bash
docker build -f services/model/Dockerfile \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 \
  -t trafficflow-model-gpu .
# run with --gpus all and HALF_PRECISION=true
```

## Depends on

| Service | Why |
|---|---|
| **database** | persist counts / jobs |
| **Redis** | publish live events |
| **backend** | optional coordination |
| **weights/** | model files (`yolo11n.pt`, …) |

## Note

Live always-on detection can also run **inside the backend** process (`AUTO_START_LIVE_STREAMS`).  
Use this worker for offline jobs / separated scale-out inference.
