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

if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import alembic, fastapi, yaml, passlib, yt_dlp, cv2, uvicorn, lap" >/dev/null 2>&1; then
  echo "Bootstrapping local virtualenv..."
  rm -rf .venv
  "$PYTHON_BIN" -m venv --system-site-packages .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -e ".[dev]"
fi

source .venv/bin/activate

export APP_ENV="${APP_ENV:-development}"
export SERVE_FRONTEND

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
