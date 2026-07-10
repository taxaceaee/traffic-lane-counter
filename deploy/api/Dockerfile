FROM python:3.10-slim

WORKDIR /app

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_PACKAGES="torch==2.7.1+cpu torchvision==0.22.1+cpu"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps for the API, including live-stream inference support.
COPY tf_core/pyproject.toml tf_core/
COPY tf_common/pyproject.toml tf_common/
COPY tf_db/pyproject.toml tf_db/
COPY tf_worker/pyproject.toml tf_worker/
COPY tf_api/pyproject.toml tf_api/
RUN pip install --no-cache-dir \
    --index-url "${TORCH_INDEX_URL}" \
    --extra-index-url https://pypi.org/simple \
    ${TORCH_PACKAGES} && \
    pip install --no-cache-dir \
    tf_core/ tf_common/ tf_db/ tf_worker/ tf_api/

# Copy package code
COPY tf_core ./tf_core
COPY tf_common ./tf_common
COPY tf_db ./tf_db
COPY tf_worker ./tf_worker
COPY tf_api ./tf_api
COPY configs ./configs
COPY weights ./weights
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

# Keep scripts for convenience
COPY scripts ./scripts

# Run as non-root user
RUN mkdir -p /app/outputs /app/storage /app/logs && \
    addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /app
USER app

# Expose API + WebSocket on same port
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/readyz')" || exit 1

CMD ["uvicorn", "tf_api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--log-level", "info"]
