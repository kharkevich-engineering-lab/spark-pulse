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
import re
from pathlib import Path

version = '${VERSION}'
path = Path('pyproject.toml')
text = path.read_text(encoding='utf-8')

project_section = re.search(r'(?ms)^\[project\]\n(.*?)(?:^\[|\Z)', text)
if not project_section:
    raise SystemExit('Could not find [project] section in pyproject.toml')

section_body = project_section.group(1)
if not re.search(r'(?m)^version\s*=\s*\"[^\"]*\"\s*$', section_body):
    raise SystemExit('Could not find version key in [project] section')

updated_section_body = re.sub(
    r'(?m)^version\s*=\s*\"[^\"]*\"\s*$',
    f'version = \"{version}\"',
    section_body,
    count=1,
)

start, end = project_section.span(1)
updated_text = text[:start] + updated_section_body + text[end:]
path.write_text(updated_text, encoding='utf-8')
"

# Build Python package
echo "Building Python package..."
python3 -m pip install --upgrade pip build
python3 -m build

echo "Release ${VERSION} complete."
