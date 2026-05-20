#!/usr/bin/env bash
# Start backend in simulation mode only.
# Usage: ./scripts/run-backend.sh [--port 8100] [--no-reload]
set -euo pipefail

port=8100
reload="--reload"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)  port="$2"; shift 2 ;;
        --no-reload) reload=""; shift ;;
        *) echo "Unknown: $1"; exit 1 ;;
    esac
done

# Create venv if needed
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    .venv/bin/pip install -e ".[dev]" -q
fi

. .venv/bin/activate

echo "Starting backend in SIMULATION mode on port $port..."
echo "  http://localhost:$port"
echo "  http://localhost:$port/docs  (Swagger UI)"

SIMULATION_MODE=1 uvicorn spark_pulse.app:app \
    --host 0.0.0.0 --port "$port" \
    $reload
