#!/usr/bin/env bash
# Start backend in production mode (real tools, no simulation, no hot reload).
# Usage: ./scripts/run-production.sh [--port 8100] [--workers 1]
set -euo pipefail

port=8100
workers=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)    port="$2"; shift 2 ;;
        --workers) workers="$2"; shift 2 ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

if [ ! -d ".venv" ]; then
    echo "Virtual environment not found. Run ./scripts/run-dev-server.sh first."
    exit 1
fi

. .venv/bin/activate

# Build UI if missing
if [ ! -f "spark_pulse/ui/index.html" ]; then
    echo "Building frontend (production requires built UI)..."
    npm --prefix web run build
fi

echo "Starting backend in PRODUCTION mode on port $port ($workers worker(s))..."
echo ""
echo "  http://localhost:$port"
echo "  http://localhost:$port/docs  (Swagger UI)"

uvicorn spark_pulse.app:app \
    --host 0.0.0.0 --port "$port" \
    --workers "$workers"
