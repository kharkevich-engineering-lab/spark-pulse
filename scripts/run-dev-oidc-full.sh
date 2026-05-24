#!/usr/bin/env bash
set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────

cleanup() {
    echo ""
    echo "Shutting down..."
    jobs -p | xargs -r kill 2>/dev/null
    wait 2>/dev/null
    echo "Done."
}
trap cleanup EXIT INT TERM

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

# Use python3.14 (project requires >=3.10)
PYTHON="${PYTHON:-python3.14}"

if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment with ${PYTHON}..."
    ${PYTHON} -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -e ".[dev]" -q

# Install frontend dependencies if needed
if [ ! -d "web/node_modules" ]; then
    echo "Installing frontend dependencies..."
    pushd web
    npm install
    popd
fi

# Build UI if not already built
if [ ! -f "spark_pulse/ui/index.html" ]; then
    echo "Building frontend..."
    pushd web
    npm run build
    popd
fi

# ── Start mock OIDC provider ─────────────────────────────────────────────────

OIDC_PORT=9400
echo "Starting mock OIDC provider on port ${OIDC_PORT}..."
oidc-provider-mock --port ${OIDC_PORT} &
OIDC_PID=$!

wait_server_ready "http://localhost:${OIDC_PORT}/.well-known/openid-configuration"
echo "✓ Mock OIDC provider is ready at http://localhost:${OIDC_PORT}"

# ── Configure app for dev SSO ────────────────────────────────────────────────

export SPARK_PULSE_AUTH_ENABLED=true
export SPARK_PULSE_MCP_ENABLED=true
export OIDC_PROVIDER_URL="http://localhost:${OIDC_PORT}"

# Create a temporary settings file for dev SSO
DEV_SETTINGS_DIR="${HOME}/.config/spark-pulse"
mkdir -p "${DEV_SETTINGS_DIR}"

cat > "${DEV_SETTINGS_DIR}/settings.json" <<EOF
{
    "auth_enabled": true,
    "oidc_provider_url": "${OIDC_PROVIDER_URL}",
    "oidc_client_id": "spark-pulse-dev",
    "webui_port": 8100,
    "mcp_enabled": true
}
EOF

# Create secrets file with a dummy client secret
cat > "${DEV_SETTINGS_DIR}/secrets.json" <<EOF
{
    "oidc_client_secret": "dev-secret"
}
EOF
chmod 600 "${DEV_SETTINGS_DIR}/secrets.json"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Spark Pulse Dev Mode (Full Stack)"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "  Mock OIDC Provider:  http://localhost:${OIDC_PORT}"
echo "  Backend + Frontend:  http://localhost:8100"
echo ""
echo "  Dev Credentials:"
echo "    Provider URL:  ${OIDC_PROVIDER_URL}"
echo "    Client ID:     spark-pulse-dev"
echo "    Client Secret: dev-secret"
echo ""
echo "  To stop: press Ctrl+C"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Start backend (serves both API and built frontend) ───────────────────────

echo "Starting backend on http://localhost:8100..."
python -m uvicorn spark_pulse.app:app --host 0.0.0.0 --port 8100 --reload
