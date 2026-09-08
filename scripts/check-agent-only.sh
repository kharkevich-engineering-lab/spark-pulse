#!/usr/bin/env bash
# Does the control plane still touch a machine directly?
#
# The rule this checks: **the control plane coordinates, agents execute.** A
# container, an image, a GPU reading, a model snapshot or a process lives on a
# node, and reaching it means naming that node and sending the operation to
# its agent — including when the node is the machine this process runs on.
#
# `tests/test_no_local_operations.py` is the ratchet; this script runs it and
# then prints what is still excused and why, because an allowlist nobody reads
# is how `system.py` kept shelling out to nvidia-smi for a release after the
# parsing had moved into the agent.
#
# Exit code is the test suite's: 0 means nothing operates on a node locally.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
fi

echo "── The ratchet ────────────────────────────────────────────────────────"
"$PYTHON" -m pytest tests/test_no_local_operations.py -q "$@"

echo
echo "── What is still excused, and why ─────────────────────────────────────"
"$PYTHON" - <<'REPORT'
import importlib.util
import pathlib

spec = importlib.util.spec_from_file_location(
    "_ratchet", pathlib.Path("tests/test_no_local_operations.py")
)
ratchet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ratchet)

for title, group in (
    ("shells out", ratchet.MAY_USE_SUBPROCESS),
    ("writes to this machine", ratchet.MAY_TOUCH_THE_FILESYSTEM),
    ("signals a process", ratchet.MAY_SIGNAL_A_PROCESS),
):
    print(f"\n{title}:")
    for name, reason in sorted(group.items()):
        print(f"  {name:<22} {reason}")

print("\nthe seam itself (a node service is built here):")
for name in sorted(ratchet.MAY_TOUCH_DOCKER_DIRECTLY):
    print(f"  {name}")

print("\n── Still answers for the control node only ─────────────────────────────")
for name, reason in sorted(ratchet.STILL_SINGLE_NODE.items()):
    print(f"\n  {name}")
    for line in __import__("textwrap").wrap(reason, 68):
        print(f"      {line}")
REPORT

echo
echo "── Where a node operation goes ────────────────────────────────────────"
"$PYTHON" - <<'ROUTES'
from spark_pulse.tools import node_service

print("container operations, over the agent:")
print("  " + ", ".join(node_service.NODE_SERVICE_METHODS))
print("\nthe machine itself, over the same agent:")
print("  " + ", ".join(node_service.NODE_MACHINE_METHODS))
print(
    "\nresolved by node_service.service_for(), which has no local branch: "
    "this process runs an agent for itself and reaches it over loopback."
)
ROUTES
