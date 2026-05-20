#!/usr/bin/env bash
# Build the frontend and place it in spark_pulse/ui/ (bundled in wheel).
# Usage: ./scripts/build-ui.sh
set -euo pipefail

if [ ! -d "web/node_modules" ]; then
    echo "Installing frontend dependencies..."
    pushd web && npm install && popd
fi

echo "Building frontend..."
npm --prefix web run build

echo "Frontend built to spark_pulse/ui/"
du -sh spark_pulse/ui/
