FROM python:3.10-slim

WORKDIR /app

ENV HOME=/tmp \
    YOLO_CONFIG_DIR=/tmp/Ultralytics \
    MPLCONFIGDIR=/tmp/matplotlib

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_PACKAGES="torch==2.7.1+cpu torchvision==0.22.1+cpu"

# System deps for OpenCV + FFmpeg RTSP decoding
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libglib2.0-0 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages (torch CPU wheels unless overridden at build time)
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

# Copy only the modules needed by the AI worker
COPY tf_core ./tf_core
COPY tf_common ./tf_common
COPY tf_db ./tf_db
COPY tf_worker ./tf_worker
COPY tf_api ./tf_api
COPY configs ./configs
COPY weights ./weights

# Run as non-root user
RUN mkdir -p /app/outputs /app/storage && \
    addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /app
USER app

# Entrypoint: run the AI inference worker process
CMD ["python", "-m", "tf_worker.worker"]
