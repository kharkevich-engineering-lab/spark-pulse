#!/usr/bin/env fish
# Run the Spark Pulse end-to-end suite the way CI runs it.
#
# Builds the UI if it is missing, starts a SIMULATION_MODE=1 backend on the
# port Playwright is configured for, runs the suite, and stops the backend
# again. An already-running backend on that port is reused and left alone.
#
# Usage: ./scripts/run-e2e-tests.sh [options]
#
# Options:
#   --headed        Run with a visible browser
#   --ui            Run with Playwright UI mode
#   --debug         Run in Playwright debug mode
#   --file FILE     Run one spec (e.g. --file tests/e2e/jobs.spec.ts)
#   --port N        Backend port (default 8100, or $E2E_PORT)
#   --help, -h      Show this help

set -l repo_root (path resolve (dirname (status filename))/..)
cd $repo_root

# Extra arguments for `playwright test`, collected as a list: an empty string
# passed positionally would read as a filter that matches every spec.
set -l pw_args
set -l port (test -n "$E2E_PORT"; and echo $E2E_PORT; or echo 8100)

set -l i 1
while test $i -le (count $argv)
  switch $argv[$i]
    case --headed
      set -a pw_args --headed
    case --ui
      set -a pw_args --ui
    case --debug
      set -a pw_args --debug
    case --file
      set i (math $i + 1)
      set -a pw_args $argv[$i]
    case --port
      set i (math $i + 1)
      set port $argv[$i]
    case --help -h
      sed -n '/^# Usage:/,/^# *--help/p' (status filename) | string replace -r '^# ?' ''
      exit 0
    case '*'
      echo "Unknown option: $argv[$i]" >&2
      exit 2
  end
  set i (math $i + 1)
end

set -l base_url "http://127.0.0.1:$port"

# ── Frontend ────────────────────────────────────────────────────────────────
# The backend refuses to serve the SPA without a build, and the suite drives
# the built SPA rather than the Vite dev server.
if not test -f spark_pulse/ui/index.html
  echo "🔨 Building the frontend..."
  pushd web
  if not test -d node_modules
    npm ci; or begin; popd; echo "❌ npm ci failed"; exit 1; end
  end
  npm run build; or begin; popd; echo "❌ Frontend build failed"; exit 1; end
  popd
end

# ── Backend ─────────────────────────────────────────────────────────────────
set -l started_backend false
set -l backend_pid ""

function __e2e_stop_backend --argument-names pid
  if test -n "$pid"
    kill $pid 2>/dev/null
    wait $pid 2>/dev/null
  end
end

if curl -sf $base_url/health >/dev/null 2>&1
  echo "✅ Reusing the backend already running at $base_url"
else
  set -l python ""
  # Not `path resolve`: that follows the symlink out of the venv and loses it.
  if test -x .venv/bin/python
    set python $repo_root/.venv/bin/python
  else if python3 -c "import uvicorn" 2>/dev/null
    set python python3
  else
    echo "❌ No environment to run the backend from."
    echo "   Create one with: python3 -m venv .venv; and .venv/bin/pip install -e \".[dev]\""
    exit 1
  end

  echo "🚀 Starting the simulation backend on port $port..."
  # Auth is forced off so a developer's ~/.config/spark-pulse/settings.json
  # cannot bounce the suite to the OIDC login page; CI has it off already.
  env SIMULATION_MODE=1 SPARK_PULSE_AUTH_ENABLED=false \
    $python -m uvicorn spark_pulse.app:app --host 127.0.0.1 --port $port --workers 1 \
    >/tmp/spark-pulse-e2e-backend.log 2>&1 &
  set backend_pid $last_pid
  set started_backend true

  set -l ready false
  for attempt in (seq 1 40)
    if curl -sf $base_url/health >/dev/null 2>&1
      set ready true
      break
    end
    sleep 0.5
  end
  if test $ready = false
    echo "❌ Backend did not become healthy at $base_url"
    tail -30 /tmp/spark-pulse-e2e-backend.log
    __e2e_stop_backend $backend_pid
    exit 1
  end
  echo "✅ Backend healthy at $base_url"
end

# ── Suite ───────────────────────────────────────────────────────────────────
echo ""
echo "🧪 Running E2E tests..."
echo ""

pushd web
if not test -d node_modules
  npm ci
end
env E2E_BASE_URL=$base_url \
  npx playwright test $pw_args --config=tests/e2e/playwright.config.ts
set -l exit_code $status
popd

if test $started_backend = true
  __e2e_stop_backend $backend_pid
end

echo ""
if test $exit_code -eq 0
  echo "✅ All E2E tests passed!"
else
  echo "❌ E2E tests failed (exit code: $exit_code)"
  echo ""
  echo "View report: cd web; and npx playwright show-report"
end

exit $exit_code
