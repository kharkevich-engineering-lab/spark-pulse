#!/usr/bin/env bash
set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────

cleanup() {
    echo ""
    echo "Shutting down..."
    jobs -p | xargs -r kill 2>/dev/null
    wait 2>/dev/null
}
trap cleanup EXIT INT TERM

python_preconfigure() {
    # Use python3.14 (project requires >=3.10)
    PYTHON="${PYTHON:-python3.14}"

    if [ ! -d ".venv" ]; then
        echo "Creating Python virtual environment with ${PYTHON}..."
        ${PYTHON} -m venv .venv
    fi
    source .venv/bin/activate
    pip install --upgrade pip -q
    pip install -e ".[dev]" -q
}

ui_preconfigure() {
    if [ ! -d "web/node_modules" ]; then
        echo "Installing frontend dependencies..."
        pushd web
        npm install
        popd
    fi
}

wait_server_ready() {
    local url=$1
    for backoff in 0 0.5 1 1 2 3; do
        sleep "$backoff"
        if curl --fail --silent --show-error --output /dev/null "$url" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

# ── Pre-configuration ────────────────────────────────────────────────────────

if ! command -v node &> /dev/null; then
    echo "Error: node is not installed. Install it first (e.g. brew install node)."
    exit 1
fi

if ! command -v npm &> /dev/null; then
    echo "Error: npm is not installed."
    exit 1
fi

python_preconfigure
source .venv/bin/activate
ui_preconfigure

# ── Build frontend once (for initial HTML) ──────────────────────────────────

echo "Building frontend for first launch..."
npm --prefix web run build

# ── Start servers ───────────────────────────────────────────────────────────

# Backend (simulation mode — mock all tools)
echo "Starting backend in SIMULATION mode (port 8100)..."
SIMULATION_MODE=1 uvicorn spark_pulse.app:app --host 0.0.0.0 --port 8100 --reload &
BACKEND_PID=$!

wait_server_ready localhost:8100/health
echo "Backend is ready"

# Frontend dev server (watches and rebuilds on changes)
echo "Starting frontend dev server (port 3000)..."
npm --prefix web run dev &
FRONTEND_PID=$!

echo ""
echo "============================================================"
echo "  Spark Manager running!"
echo "  Frontend: http://localhost:3000"
echo "  Backend:  http://localhost:8100"
echo "  Press Ctrl+C to stop"
echo "============================================================"

# Wait for both
wait
