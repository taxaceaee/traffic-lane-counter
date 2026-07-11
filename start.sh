#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
# Prefer 3.10/3.11 for torch/ultralytics wheels; fall back to python3.
if [ -z "${PYTHON_BIN:-}" ]; then
  if command -v python3.10 >/dev/null 2>&1; then
    PYTHON_BIN=python3.10
  elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN=python3.11
  else
    PYTHON_BIN=python3
  fi
fi
HOST="${HOST:-0.0.0.0}"
PORT_WAS_EXPLICIT=0
if [ -n "${PORT:-}" ]; then
  PORT_WAS_EXPLICIT=1
fi
PORT="${PORT:-8000}"
SERVE_FRONTEND="${SERVE_FRONTEND:-true}"

BOOTSTRAP_REQUIRED=0
USE_GPU=0
if [ "${FORCE_CPU:-false}" != "true" ] && command -v nvidia-smi >/dev/null 2>&1 \
    ; then
  USE_GPU=1
fi
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import alembic, fastapi, yaml, passlib, yt_dlp, cv2, uvicorn; import yt_dlp_plugins.extractor.getpot_bgutil_http" >/dev/null 2>&1; then
  BOOTSTRAP_REQUIRED=1
elif [ "${FORCE_CPU:-false}" != "true" ] && command -v nvidia-smi >/dev/null 2>&1 \
    && ! .venv/bin/python -c "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)" >/dev/null 2>&1; then
  echo "Existing venv has no usable CUDA Torch; rebuilding it for GPU inference."
  BOOTSTRAP_REQUIRED=1
fi

if [ "$BOOTSTRAP_REQUIRED" -eq 1 ]; then
  echo "Bootstrapping local virtualenv..."
  rm -rf .venv
  # Always isolate project packages. GPU hosts receive an explicit CUDA wheel;
  # this avoids mixing incompatible distro NumPy/OpenCV/Torch ABIs.
  "$PYTHON_BIN" -m venv .venv
  # Use the host CA bundle explicitly; some pip builds lose their vendored
  # certifi path while bootstrapping a fresh venv.
  export PIP_CERT="${PIP_CERT:-/etc/ssl/certs/ca-certificates.crt}"
  .venv/bin/python -m pip install --upgrade pip --cert "$PIP_CERT"
  # Prefer the installed NVIDIA stack for local inference. Override with
  # FORCE_CPU=true only on a machine without a usable NVIDIA runtime.
  if [ "$USE_GPU" -eq 1 ]; then
    echo "NVIDIA GPU detected; installing isolated CUDA 12.1 Torch wheels."
    .venv/bin/python -m pip install \
      --index-url https://download.pytorch.org/whl/cu121 \
      --extra-index-url https://pypi.org/simple \
      "torch==2.5.1+cu121" "torchvision==0.20.1+cu121" --cert "$PIP_CERT"
    .venv/bin/python -m pip install -e ".[dev]" --cert "$PIP_CERT"
    export HALF_PRECISION="${HALF_PRECISION:-true}"
  else
    echo "No NVIDIA GPU selected; installing CPU Torch wheels."
    .venv/bin/python -m pip install \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url https://pypi.org/simple \
      "torch==2.5.1+cpu" "torchvision==0.20.1+cpu" --cert "$PIP_CERT"
    .venv/bin/python -m pip install -e ".[dev]" --cert "$PIP_CERT"
    export HALF_PRECISION="${HALF_PRECISION:-false}"
  fi
fi

source .venv/bin/activate

# Python packages live under packages/ (services/ is deploy boundary only).
export PYTHONPATH="${PWD}/packages${PYTHONPATH:+:$PYTHONPATH}"

export APP_ENV="${APP_ENV:-development}"
export SERVE_FRONTEND
# Local runtime data belongs under the user-writable data directory. Docker
# overrides this with its named /app/storage volume.
export STORAGE_ROOT="${STORAGE_ROOT:-data/storage}"
export OUTPUT_DIR="${OUTPUT_DIR:-data/outputs}"
export FRONTEND_DIR="${FRONTEND_DIR:-${PWD}/services/frontend}"

# YouTube now uses JS challenges and Proof-of-Origin tokens.  Keep the
# provider local to this development process so `npm run dev` is self-contained
# and no token/cookie is written into the repository.
YTDLP_POT_PROVIDER="${YTDLP_POT_PROVIDER:-true}"
YTDLP_BGUTIL_PORT="${YTDLP_BGUTIL_PORT:-4416}"
export YTDLP_POT_PROVIDER YTDLP_BGUTIL_PORT
POT_PROVIDER_PID=""

if [ "$APP_ENV" = "development" ] \
    && [ "${YOUTUBE_USE_BROWSER_COOKIES:-true}" != "false" ] \
    && [ -z "${YOUTUBE_COOKIES_FILE:-}" ] \
    && [ -z "${YOUTUBE_COOKIES_FROM_BROWSER:-}" ] \
    && [ -f "$HOME/.config/google-chrome/Default/Cookies" ]; then
  export YOUTUBE_COOKIES_FROM_BROWSER="chrome:Default"
  echo "YouTube cookies: using local Chrome Default profile (not stored in repo)."
fi

port_is_open() {
  .venv/bin/python - "$1" <<'PY'
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    sys.exit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

start_pot_provider() {
  if [ "$YTDLP_POT_PROVIDER" = "false" ] || ! command -v node >/dev/null 2>&1; then
    return 0
  fi

  local provider_home="${YTDLP_BGUTIL_SERVER_HOME:-$HOME/bgutil-ytdlp-pot-provider}"
  local provider_server="$provider_home/server"
  local provider_build="$provider_server/build/main.js"

  if [ ! -f "$provider_build" ]; then
    if ! command -v git >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
      echo "WARNING: Node/npm/git unavailable; YouTube PO-token provider is disabled."
      return 0
    fi
    echo "Preparing local YouTube PO-token provider..."
    if [ ! -d "$provider_home/.git" ]; then
      git clone --single-branch --branch 1.3.1 --depth 1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$provider_home" \
        || { echo "WARNING: Could not clone YouTube PO-token provider."; return 0; }
    fi
    (cd "$provider_server" && npm ci && npx tsc) \
      || { echo "WARNING: Could not build YouTube PO-token provider."; return 0; }
  fi

  if port_is_open "$YTDLP_BGUTIL_PORT"; then
    echo "YouTube PO-token provider already running on 127.0.0.1:${YTDLP_BGUTIL_PORT}."
    return 0
  fi

  echo "Starting local YouTube PO-token provider on 127.0.0.1:${YTDLP_BGUTIL_PORT}..."
  (
    cd "$provider_server"
    exec node build/main.js --port "$YTDLP_BGUTIL_PORT"
  ) >"${YTDLP_POT_PROVIDER_LOG:-/tmp/trafficflow-bgutil-pot.log}" 2>&1 &
  POT_PROVIDER_PID=$!
  for _ in $(seq 1 20); do
    if port_is_open "$YTDLP_BGUTIL_PORT"; then
      return 0
    fi
    sleep 0.25
  done
  echo "WARNING: YouTube PO-token provider did not become ready; extractor will report diagnostics."
}

start_pot_provider

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

# Keep Uvicorn owned by this entrypoint so Ctrl+C/SIGTERM from npm, an IDE, or
# a process supervisor also reaches the child and no orphan server keeps the
# selected development port occupied on the next `npm run dev`.
uvicorn tf_api.main:app --host "$HOST" --port "$PORT" &
SERVER_PID=$!

cleanup_server() {
  trap - INT TERM EXIT
  if [ -n "$POT_PROVIDER_PID" ] && kill -0 "$POT_PROVIDER_PID" 2>/dev/null; then
    kill -TERM "$POT_PROVIDER_PID" 2>/dev/null || true
    wait "$POT_PROVIDER_PID" 2>/dev/null || true
  fi
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}

trap cleanup_server INT TERM EXIT
server_status=0
if wait "$SERVER_PID"; then
  server_status=$?
else
  server_status=$?
fi
trap - EXIT
# Ctrl+C/SIGTERM is an intentional development shutdown, not an application
# failure. Returning zero keeps npm/IDE restart tasks from reporting a false
# error while preserving every other Uvicorn exit code.
if [ "$server_status" -eq 130 ] || [ "$server_status" -eq 143 ]; then
  server_status=0
fi
exit "$server_status"
