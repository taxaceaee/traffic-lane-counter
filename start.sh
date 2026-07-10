#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${HOST:-0.0.0.0}"
PORT_WAS_EXPLICIT=0
if [ -n "${PORT:-}" ]; then
  PORT_WAS_EXPLICIT=1
fi
PORT="${PORT:-8000}"
SERVE_FRONTEND="${SERVE_FRONTEND:-true}"

BOOTSTRAP_REQUIRED=0
USE_SYSTEM_GPU=0
if [ "${FORCE_CPU:-false}" != "true" ] && command -v nvidia-smi >/dev/null 2>&1 \
    && "$PYTHON_BIN" -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" >/dev/null 2>&1; then
  USE_SYSTEM_GPU=1
fi
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import alembic, fastapi, yaml, passlib, yt_dlp, cv2, uvicorn, lap" >/dev/null 2>&1; then
  BOOTSTRAP_REQUIRED=1
elif [ "${FORCE_CPU:-false}" != "true" ] && command -v nvidia-smi >/dev/null 2>&1 \
    && ! .venv/bin/python -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" >/dev/null 2>&1; then
  echo "Existing venv has no usable CUDA Torch; rebuilding it for GPU inference."
  BOOTSTRAP_REQUIRED=1
fi

if [ "$BOOTSTRAP_REQUIRED" -eq 1 ]; then
  echo "Bootstrapping local virtualenv..."
  rm -rf .venv
  # On a GPU host, reuse the already-installed, working CUDA Torch/OpenCV ABI
  # instead of downloading a second multi-GB CUDA stack into the venv.
  # CPU hosts use a fully isolated venv to avoid distro ABI mixing.
  if [ "$USE_SYSTEM_GPU" -eq 1 ]; then
    "$PYTHON_BIN" -m venv --system-site-packages .venv
  else
    "$PYTHON_BIN" -m venv .venv
  fi
  # Use the host CA bundle explicitly; some pip builds lose their vendored
  # certifi path while bootstrapping a fresh venv.
  export PIP_CERT="${PIP_CERT:-/etc/ssl/certs/ca-certificates.crt}"
  .venv/bin/python -m pip install --upgrade pip --cert "$PIP_CERT"
  # Prefer the installed NVIDIA stack for local inference. Override with
  # FORCE_CPU=true only on a machine without a usable NVIDIA runtime.
  if [ "$USE_SYSTEM_GPU" -eq 1 ]; then
    echo "NVIDIA GPU detected; reusing system CUDA 12.1 Torch wheels."
    # Torch, torchvision, ultralytics, NumPy and OpenCV come from the verified
    # system GPU environment. Install only the API/runtime packages absent from
    # it, then install this project without resolving heavy dependencies.
    .venv/bin/python -m pip install --no-cache-dir \
      'fastapi>=0.110,<0.111' 'uvicorn[standard]>=0.29,<0.31' pydantic \
      python-multipart 'redis>=5.0' yt-dlp alembic \
      slowapi passlib[bcrypt] 'bcrypt>=4.0,<4.1' prometheus-client pytest pytest-timeout \
      --cert "$PIP_CERT"
    .venv/bin/python -m pip install --no-deps -e ".[dev]" --cert "$PIP_CERT"
    export HALF_PRECISION="${HALF_PRECISION:-true}"
  else
    echo "No NVIDIA GPU selected; installing CPU Torch wheels."
    .venv/bin/python -m pip install \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url https://pypi.org/simple \
      torch==2.7.1+cpu torchvision==0.22.1+cpu --cert "$PIP_CERT"
    .venv/bin/python -m pip install -e ".[dev]" --cert "$PIP_CERT"
    export HALF_PRECISION="${HALF_PRECISION:-false}"
  fi
fi

source .venv/bin/activate

export APP_ENV="${APP_ENV:-development}"
export SERVE_FRONTEND
# Local runtime data belongs under the user-writable data directory. Docker
# overrides this with its named /app/storage volume.
export STORAGE_ROOT="${STORAGE_ROOT:-data/storage}"
export OUTPUT_DIR="${OUTPUT_DIR:-data/outputs}"

# IDEs and other local projects frequently occupy 8000. A default development
# launch should remain usable in that situation; an explicitly supplied PORT
# must still fail loudly so deployment mistakes are not hidden.
if [ "$APP_ENV" = "development" ] && [ "$PORT_WAS_EXPLICIT" -eq 0 ]; then
  if ! .venv/bin/python - "$HOST" "$PORT" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    sys.exit(0 if sock.connect_ex((probe_host, port)) != 0 else 1)
PY
  then
    original_port="$PORT"
    for candidate in $(seq $((PORT + 1)) $((PORT + 20))); do
      if .venv/bin/python - "$HOST" "$candidate" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    sys.exit(0 if sock.connect_ex((probe_host, port)) != 0 else 1)
PY
      then
        PORT="$candidate"
        echo "Port ${original_port} is busy; using available development port ${PORT}."
        break
      fi
    done
  fi
fi

if [ -z "${JWT_SECRET:-}" ]; then
  export JWT_SECRET="trafficflow_local_dev_secret_change_me"
  echo "JWT_SECRET not set; using local development fallback."
fi

# A fresh local database has no user to log into the SPA with. Keep this
# deterministic and development-only; staging/production must provide an
# explicit bootstrap password and is rejected by the auth configuration when
# the JWT secret is unsafe.
if [ "$APP_ENV" = "development" ] && [ -z "${BOOTSTRAP_ADMIN_PASSWORD:-}" ]; then
  export BOOTSTRAP_ADMIN_PASSWORD="${DEV_BOOTSTRAP_ADMIN_PASSWORD:-admin123}"
  echo "Development bootstrap login: admin / ${BOOTSTRAP_ADMIN_PASSWORD}"
fi

echo "Starting API server (${HOST}:${PORT})..."

echo ""
echo "======================================"
echo "  API:       http://localhost:${PORT}"
echo "  API:       http://localhost:${PORT}/api/health"
echo "  Docs:      http://localhost:${PORT}/docs"
if [ "${SERVE_FRONTEND}" = "true" ]; then
  echo "  SPA:       http://localhost:${PORT}"
fi
echo "======================================"
echo ""
echo "Press Ctrl+C to stop the server"

# Keep Uvicorn as the foreground process owned by npm. This preserves the
# real exit code and forwards terminal signals correctly in IDE terminals.
exec uvicorn tf_api.main:app --host "$HOST" --port "$PORT"
