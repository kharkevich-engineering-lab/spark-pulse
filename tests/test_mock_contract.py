"""The contract between every ``tools/`` module and its ``mock/`` twin.

`CLAUDE.md`: "Each module in `tools/` needs a same-named twin in `mock/`."
What that has meant in practice is a file with the same *name*, not the same
*API* — and because consumers reach these modules through the switch
(`tools.<name>.<thing>`), a name the mock does not have is not a type error, it
is a 500 the moment a simulated request touches that code path. Three such
faults were live when this file was written:

* ``mock/mods.py`` had no ``ModOrchestrator``/``ModDeployment``, so
  ``/api/mods/apply`` and ``/api/mods/rollback`` could only ever return 500.
* ``mock/health.py`` shared *no* names at all with ``tools/health.py``, so
  every ``/api/health/*`` endpoint answered with its own ``AttributeError``.
  (That module is gone: the health monitor it belonged to never ran, and the
  engine-metrics sampler replaced it.)
* ``mock/launch_script.py`` had none of the four names
  ``routers/launch_script.py`` calls.

Each of those is a 500 a unit test would never have seen and an e2e failure
nobody would have understood. This file is the test that catches the next one:
it is a ratchet, not a wish. New drift fails; the gaps that remain are listed
below with what they cost, and the list may only get shorter.

The comparison runs in a **fresh interpreter** on purpose. Reaching a real
submodule from inside the suite means importing it, and that import rebinds
``spark_pulse.tools.<name>`` for the rest of the process — several existing
tests depend on exactly that side effect, and undoing it breaks them. A
subprocess sees the switch as the application does at startup, and leaves this
process's import graph untouched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Names a real module defines that its twin still lacks.
#:
#: Every entry is a real defect, not an exemption on principle — nothing in
#: simulation may reach these names. They are listed rather than fixed because
#: fixing each one needs a judgement about what the simulated version should
#: *do*, which is a change to the simulator, not to this test.
KNOWN_GAPS: dict[str, set[str]] = {
    # Container metadata and the two pull-failure signals: mock/docker.py
    # subclasses the real DockerService and re-exports only part of its
    # vocabulary.
    "docker": {"ContainerMetadata", "ExecResult", "PullCancelled", "PullStalled"},
    # The local hub-cache path helper.
    "models": {"local_repo_path"},
    # mock/native_runtime.py delegates wholesale and misses one accessor.
    "native_runtime": {"logs_for_container"},
    # The two host probes pre-flight injects; the mock simulates at a higher
    # level and never exposes them.
    "preflight": {"LocalHostProbe", "SSHHostProbe"},
}

_PROBE = """
import importlib, json, sys
from spark_pulse import tools

gaps = {}
for name in sorted(n for n in dir(tools) if not n.startswith("_")):
    twin = getattr(tools, name)
    module_name = getattr(twin, "__name__", "")
    # Only the modules the switch actually swaps: a real-only module is bound
    # to itself and has no twin to keep up.
    if not module_name.startswith("spark_pulse.mock."):
        continue
    real = importlib.import_module("spark_pulse.tools." + name)
    defined = {
        n
        for n, v in vars(real).items()
        if not n.startswith("_")
        and getattr(v, "__module__", None) == real.__name__
    }
    gaps[name] = sorted(n for n in defined if not hasattr(twin, n))
json.dump(gaps, sys.stdout)
"""


def _gaps() -> dict[str, list[str]]:
    """What each simulated module is missing, as a fresh interpreter sees it."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env={"SIMULATION_MODE": "1", "PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


GAPS = _gaps()


def test_every_twin_the_package_ships_is_checked():
    """A guard on the guard: a shrunken list would make the rest vacuous."""
    assert len(GAPS) > 15
    assert {"mods", "engine_metrics", "recipes", "docker", "launch_script"} <= set(GAPS)


@pytest.mark.parametrize("name", sorted(GAPS))
def test_the_mock_answers_to_every_name_its_real_twin_defines(name):
    missing = set(GAPS[name])
    known = KNOWN_GAPS.get(name, set())

    assert missing <= known, (
        f"spark_pulse.mock.{name} is missing {sorted(missing - known)}, which a "
        f"caller reaching tools.{name} through the simulation switch would hit "
        f"as an AttributeError at runtime"
    )


@pytest.mark.parametrize("name", sorted(KNOWN_GAPS))
def test_a_closed_gap_is_removed_from_the_list(name):
    """The ratchet: once a gap is fixed, it may not sit here pretending to exist."""
    still_missing = set(GAPS[name]) & KNOWN_GAPS[name]

    assert still_missing == KNOWN_GAPS[name], (
        f"spark_pulse.mock.{name} now has "
        f"{sorted(KNOWN_GAPS[name] - still_missing)} — delete it from KNOWN_GAPS"
    )
