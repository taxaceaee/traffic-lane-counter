# TrafficFlow Model / Inference Worker
# Build from repo root:
#   docker build -f services/model/Dockerfile -t trafficflow-model .
FROM python:3.10-slim

WORKDIR /app

ENV HOME=/tmp \
    YOLO_CONFIG_DIR=/tmp/Ultralytics \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONPATH=/app/packages

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_PACKAGES="torch==2.7.1+cpu torchvision==0.22.1+cpu"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY packages/tf_core/pyproject.toml packages/tf_core/
COPY packages/tf_common/pyproject.toml packages/tf_common/
COPY packages/tf_db/pyproject.toml packages/tf_db/
COPY packages/tf_worker/pyproject.toml packages/tf_worker/
COPY packages/tf_api/pyproject.toml packages/tf_api/
RUN pip install --no-cache-dir \
    --index-url "${TORCH_INDEX_URL}" \
    --extra-index-url https://pypi.org/simple \
    ${TORCH_PACKAGES} && \
    pip install --no-cache-dir \
    packages/tf_core/ packages/tf_common/ packages/tf_db/ packages/tf_worker/ packages/tf_api/

COPY packages ./packages
COPY configs ./configs
COPY weights ./weights

RUN mkdir -p /app/outputs /app/storage && \
    addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /app
USER app

CMD ["python", "-m", "tf_worker.worker"]
