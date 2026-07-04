#!/bin/bash
set -e

cd "$(dirname "$0")"
source .venv/bin/activate

echo "Starting API server (port 8000)..."
uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
API_PID=$!

echo "Starting Dashboard (port 8501)..."
streamlit run frontend/app.py --server.port=8501 --server.headless=true &
DASH_PID=$!

echo ""
echo "======================================"
echo "  API:       http://localhost:8000"
echo "  Dashboard: http://localhost:8501"
echo "  Docs:      http://localhost:8000/docs"
echo "======================================"
echo ""
echo "Press Ctrl+C to stop both services"

trap "kill $API_PID $DASH_PID 2>/dev/null; exit" SIGINT SIGTERM
wait
