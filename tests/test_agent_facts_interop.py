"""Interface classification, pinned from the control plane's side.

The agent classifies interfaces to build a node's hardware fingerprint, and
the control plane classifies them to fill that node's record. Two
implementations of one naming rule, in two languages, and the fingerprint
*excludes* docker interfaces — so a disagreement about which names are docker
changes the fingerprint, and a node whose fingerprint moves reports a reimage
on every heartbeat.

`agent/tests/fixtures/interface-classes.json` was generated from
``spark_pulse.tools.discovery._classify_interface``. This asserts the fixture
still matches that function, and `agent/tests/facts_interop.rs` asserts the
agent matches the same fixture. Neither side can change alone without one of
them going red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spark_pulse.tools.discovery import _classify_interface

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "agent"
    / "tests"
    / "fixtures"
    / "interface-classes.json"
)


def cases() -> dict[str, str]:
    return json.loads(FIXTURE.read_text())["classes"]


def test_the_fixture_still_matches_what_the_control_plane_computes():
    """The fixture is generated, not hand-written; this is the ratchet.

    If someone changes ``_classify_interface`` — adds a prefix, reorders the
    branches — this fails here, and they are told to regenerate the fixture,
    which then fails the agent's side until it is changed too. That is the
    whole mechanism: one fixture, two implementations, no way to move one.
    """
    for name, expected in cases().items():
        assert _classify_interface(name) == expected, (
            f"{name!r} is now {_classify_interface(name)!r} here but {expected!r} "
            "in the fixture the agent is tested against. Regenerate the fixture "
            "and check the agent still agrees."
        )


def test_the_fixture_exercises_every_branch():
    """A shrunken fixture would make the agreement above vacuous."""
    found = set(cases().values())
    assert found == {"loopback", "docker", "infiniband", "ethernet", "other"}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("lo", "loopback"),
        ("docker0", "docker"),
        ("br-1a2b3c", "docker"),
        ("ib0", "infiniband"),
        ("mlx5_0", "infiniband"),
        ("enp1s0", "ethernet"),
        ("eth0", "ethernet"),
        # A RoCE *device* is not a netdev and is not classified as one. The
        # two are routinely confused — `rocep1s0f1` is what NCCL_IB_HCA
        # selects, `enp1s0np1` is the interface it drives — and calling the
        # device "infiniband" here would put it in the fingerprint twice.
        ("rocep1s0f1", "other"),
        ("veth1a2b3c", "other"),
    ],
)
def test_the_rules_themselves(name: str, expected: str):
    """Stated directly, so the intent survives a regenerated fixture."""
    assert _classify_interface(name) == expected
