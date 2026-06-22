#!/usr/bin/env fish
# Run Spark Pulse E2E tests
# Usage: ./scripts/run-e2e-tests.sh [options]
#
# Options:
#   --headed      Run with visible browser
#   --ui          Run with Playwright UI mode
#   --debug       Run in debug mode
#   --file FILE   Run specific test file
#   --help        Show this help message

set -l headed ""
set -l ui_mode ""
set -l debug ""
set -l file ""
set -l help false

for arg in $argv
  switch $arg
    case --headed
      set headed "--headed"
    case --ui
      set ui_mode "--ui"
    case --debug
      set debug "--debug"
    case --file
      set file $argv[(contains -- $arg $argv | tr ' ' '\n' | grep -n $arg | cut -d: -f1 | xargs math + 1)]
    case --help -h
      set help true
  end
end

if test $help = true
  echo "Run Spark Pulse E2E tests"
  echo ""
  echo "Usage: $0 [options]"
  echo ""
  echo "Options:"
  echo "  --headed      Run with visible browser"
  echo "  --ui          Run with Playwright UI mode"
  echo "  --debug       Run in debug mode"
  echo "  --file FILE   Run specific test file"
  echo "  --help        Show this help message"
  exit 0
end

# Check if backend is running
if ! curl -s http://127.0.0.1:8100/health > /dev/null 2>&1
  echo "❌ Backend server is not running at http://127.0.0.1:8100"
  echo "   Start it with: ./scripts/run-backend.sh"
  exit 1
end

echo "✅ Backend server is running"
echo ""

# Build frontend if needed
if not test -d spark_pulse/ui
  echo "🔨 Building frontend..."
  cd web
  npm run build
  cd ..
end

# Run tests
echo "🧪 Running E2E tests..."
echo ""

cd web
npx playwright test $headed $ui_mode $debug $file --config=tests/e2e/playwright.config.ts
set -l exit_code $status

echo ""
if test $exit_code -eq 0
  echo "✅ All E2E tests passed!"
else
  echo "❌ E2E tests failed (exit code: $exit_code)"
  echo ""
  echo "View report: npx playwright show-report"
end

exit $exit_code
