# TrafficFlow Backend API
# Build from repo root:
#   docker build -f services/backend/Dockerfile -t trafficflow-api .
FROM python:3.10-slim

WORKDIR /app

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_PACKAGES="torch==2.7.1+cpu torchvision==0.22.1+cpu"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONPATH=/app/packages

# Dependency layer (package metadata only for cache)
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

# Application code
COPY packages ./packages
COPY configs ./configs
COPY weights ./weights
COPY services/database/alembic.ini ./services/database/alembic.ini
COPY services/database/alembic ./services/database/alembic
COPY scripts ./scripts

RUN mkdir -p /app/outputs /app/storage /app/logs && \
    addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/readyz')" || exit 1

CMD ["uvicorn", "tf_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
