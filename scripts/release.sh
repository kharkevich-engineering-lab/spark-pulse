#!/usr/bin/env bash
set -euo pipefail

VERSION=${1:?Usage: release.sh <version>}

# Build frontend
echo "Building frontend for version ${VERSION}..."
cd web
npm ci
npm run build
cd ..

# Set version in pyproject.toml
echo "Setting version to ${VERSION}..."
python3 -c "
import tomllib, tomli_w
with open('pyproject.toml', 'rb') as f:
    cfg = tomllib.load(f)
cfg['project']['version'] = '${VERSION}'
with open('pyproject.toml', 'w') as f:
    tomli_w.dump(cfg, f)
"

# Build Python package
echo "Building Python package..."
python3 -m pip install --upgrade pip build
python3 -m build

echo "Release ${VERSION} complete."
