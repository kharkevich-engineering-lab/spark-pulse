#!/usr/bin/env bash
# Start backend in production mode (real tools, no simulation, no hot reload).
# Usage: ./scripts/run-production.sh [--port 8100] [--workers 1]
#
# Binds loopback: with auth off (the default) the API answers unauthenticated
# callers, so exposing it to the LAN needs a deliberate step. To reach it from
# other hosts, enable auth (SPARK_PULSE_AUTH_ENABLED=true + OIDC config) and
# bind a real interface, or set SPARK_PULSE_ALLOW_INSECURE_BIND=1 to accept the
# exposure — the process refuses a non-loopback bind with auth off otherwise.
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
    --host 127.0.0.1 --port "$port" \
    --workers "$workers"
