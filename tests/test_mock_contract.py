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

A name is necessary but not sufficient. A twin can answer to every name its
real module defines and still take the wrong arguments — a parameter renamed,
dropped, or newly required — and *that* is a ``TypeError`` the moment a real
caller reaches the mock through the switch, which the name check sails past.
So the same fresh interpreter also compares, for every public callable both
modules define, whether the mock's signature accepts the calls the real one's
permits (:data:`KNOWN_SIGNATURE_DRIFT` for the intended exceptions). It is
pragmatic about ``*args``/``**kwargs`` delegates — those absorb what they cover
— so a legitimate wrapper is left alone and only genuine drift fails.

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
#: An entry earns its place only by being unreachable in simulation by
#: construction, not merely unfinished — the container-metadata/exec/pull
#: signals, the log accessor and the hub-cache path helper that once sat here
#: were closed by teaching the mock to answer them, because a caller *could*
#: reach them. What remains is a design boundary, called out below.
KNOWN_GAPS: dict[str, set[str]] = {
    # ``AgentHostProbe`` is the *transport* the pre-flight uses in production —
    # a ``RunHostProbe`` op to a node's own agent. Simulation deliberately does
    # not re-export it: ``mock.preflight`` swaps the transport at one seam
    # (``probe_for`` returns ``SimulatedHostProbe``) so every check runs the
    # real code over invented bytes, and nothing in simulation ever resolves
    # ``AgentHostProbe``. A stand-in here would be a second transport that the
    # simulated path never takes — worse than the honest absence. This is the
    # one entry that is a design boundary, not an unfinished twin, so it stays.
    "preflight": {"AgentHostProbe"},
}

#: Public callables whose simulated signature is allowed to differ from the
#: real one — the same ratchet the gap list is: a mock whose arity or
#: parameters drifted from the function it stands in for accepts calls the real
#: one refuses (or refuses calls the real one accepts), which is the same
#: 500-in-simulation the name check catches one step earlier. A drift that is
#: genuinely intended goes here with its reason.
KNOWN_SIGNATURE_DRIFT: dict[str, set[str]] = {
    # ``mock.ssh`` has one client and it is simulated, so it aliases
    # ``OpenSSHClient = SSHClient`` on purpose (``test_mock_transports.py``
    # pins that identity). The mock client's constructor is a test double's
    # (canned returncode/stdout, fail-hosts), not the real one's
    # (user/identity_file/host_key_policy). Nothing in simulation constructs it
    # with the real kwargs — the one caller that does (``models._make_ssh_client``)
    # is shadowed by ``mock.models`` — so the divergence is the design, not drift.
    "ssh": {"OpenSSHClient"},
}

# Emits, from a fresh interpreter that sees the switch as startup does:
#   gaps: {module: [names the twin lacks]}
#   sig:  {module: ["name: why the twin's signature is incompatible"]}
# A signature is "incompatible" when a call the real function's signature
# permits would not bind against the mock's — a real caller reaches the mock
# through the switch, so the mock has to accept at least what the real one
# does. ``*args``/``**kwargs`` wrappers are treated as accepting anything they
# cover, so a legitimate ``def f(**kwargs)`` delegate is not flagged; a renamed,
# dropped, or newly-required parameter is.
_PROBE = r"""
import importlib, inspect, json, sys
from spark_pulse import tools

P = inspect.Parameter
_VAR = (P.VAR_POSITIONAL, P.VAR_KEYWORD)


def _named(sig):
    return [p for n, p in sig.parameters.items() if n != "self" and p.kind not in _VAR]


def _incompatible(real, mock):
    "Why the mock signature would reject a call the real one permits, or None."
    rp, mp = _named(real), _named(mock)
    kinds = [p.kind for p in mock.parameters.values()]
    has_var_kw = P.VAR_KEYWORD in kinds
    has_var_pos = P.VAR_POSITIONAL in kinds
    mock_names = {p.name for p in mp}
    # (A) Every parameter a real caller may pass has to be bindable on the mock.
    for p in rp:
        if p.name in mock_names:
            continue
        if p.kind == P.POSITIONAL_ONLY:
            if has_var_pos:
                continue
        elif has_var_kw or has_var_pos:
            continue
        return f"drops parameter {p.name!r}"
    # (B) The mock must not demand a parameter the real signature never supplies.
    real_names = {p.name for p in rp}
    for p in mp:
        if p.default is not P.empty or p.name in real_names:
            continue
        return f"requires parameter {p.name!r} the real function does not"
    return None


gaps, sig = {}, {}
for name in sorted(n for n in dir(tools) if not n.startswith("_")):
    twin = getattr(tools, name)
    module_name = getattr(twin, "__name__", "")
    # Only the modules the switch actually swaps: a real-only module is bound
    # to itself and has no twin to keep up.
    if not module_name.startswith("spark_pulse.mock."):
        continue
    real = importlib.import_module("spark_pulse.tools." + name)
    defined = {
        n: v
        for n, v in vars(real).items()
        if not n.startswith("_")
        and getattr(v, "__module__", None) == real.__name__
    }
    gaps[name] = sorted(n for n in defined if not hasattr(twin, n))
    issues = []
    for attr, real_obj in sorted(defined.items()):
        mock_obj = getattr(twin, attr, None)
        # A re-exported name is the same object — trivially compatible.
        if mock_obj is None or mock_obj is real_obj:
            continue
        if not (inspect.isfunction(mock_obj) or inspect.isclass(mock_obj)):
            continue
        if not (inspect.isfunction(real_obj) or inspect.isclass(real_obj)):
            continue
        try:
            real_sig = inspect.signature(real_obj)
            mock_sig = inspect.signature(mock_obj)
        except (ValueError, TypeError):
            continue
        why = _incompatible(real_sig, mock_sig)
        if why:
            issues.append(f"{attr}: {why}")
    sig[name] = sorted(issues)
json.dump({"gaps": gaps, "sig": sig}, sys.stdout)
"""


def _probe() -> dict[str, dict[str, list[str]]]:
    """What a fresh interpreter sees: missing names and signature drift."""
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


_PROBED = _probe()
GAPS = _PROBED["gaps"]
SIG = _PROBED["sig"]


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


@pytest.mark.parametrize("name", sorted(SIG))
def test_the_mock_signature_matches_the_name_it_stands_in_for(name):
    """A name is not enough: a mock whose parameters drifted from the real
    function is a call that binds in production and raises ``TypeError`` in
    simulation — a name-presence check sails straight past it. Every public
    callable both modules define must accept the calls the real signature
    permits."""
    issues = set(SIG[name])
    allowed = {
        i
        for i in issues
        if i.split(":", 1)[0] in KNOWN_SIGNATURE_DRIFT.get(name, set())
    }

    assert issues <= allowed, (
        f"spark_pulse.mock.{name} has drifted from its real twin: "
        f"{sorted(issues - allowed)} — a caller reaching tools.{name} through "
        f"the simulation switch would hit this as a TypeError at runtime"
    )
