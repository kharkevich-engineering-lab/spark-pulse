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

# The node agent has to be in the wheel before the wheel is built.
#
# This guard exists because its absence is *silent*: `python -m build` happily
# produces a wheel with an empty `spark_pulse/agent/bin/`, and nothing goes
# wrong until an operator installs it — at which point every node install
# raises MissingAgentBinary and, worse, the control plane cannot start an agent
# for itself and so refuses to boot at all. A release that cannot run is not a
# release, and it must not be possible to cut one by forgetting a step.
#
# The binaries are built natively, per architecture, by the release workflow's
# `agent` matrix (see .github/workflows/release.yml) and downloaded here.
# `scripts/build-agent.sh` refuses to build under emulation, so this script
# deliberately does not try to build them itself: on an x86_64 release runner
# it could only produce the wrong thing or nothing.
AGENT_BIN="spark_pulse/agent/bin"
REQUIRED_AGENT="${REQUIRED_AGENT:-spark-pulse-agent-aarch64-unknown-linux-musl}"
if [[ ! -f "${AGENT_BIN}/${REQUIRED_AGENT}" ]]; then
  {
    echo "release: ${AGENT_BIN}/${REQUIRED_AGENT} is missing."
    echo
    echo "  The wheel would install and then fail on first use: every node"
    echo "  install raises MissingAgentBinary, and a production control plane"
    echo "  refuses to boot because it cannot start an agent for itself."
    echo
    echo "  Build it on an aarch64 machine with ./scripts/build-agent.sh, or"
    echo "  let the release workflow's agent job produce it."
  } >&2
  exit 1
fi
echo "Agent binaries in the wheel:"
for binary in "${AGENT_BIN}"/spark-pulse-agent-*; do
  echo "  $(basename "$binary")  $(du -h "$binary" | cut -f1)"
done

# Build Python package
echo "Building Python package..."
python3 -m pip install --upgrade pip build
python3 -m build

echo "Release ${VERSION} complete."
