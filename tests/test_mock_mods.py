"""Tests for ``mock/mods.py`` — the mods the simulation serves.

The mock package is what the Playwright suite and every ``SIMULATION_MODE=1``
run drive, so a mock that has drifted from its real twin shows up as a
mysterious e2e failure rather than a unit-test failure. This file pins both
halves: the canned data the UI renders, and the API surface the switch
promises callers is the same in either mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spark_pulse.mock import mods


class _Node:
    def __init__(self, ip: str):
        self.ip = ip
        self.container_name = f"{ip}-container"


class _Cluster:
    def __init__(self):
        self.head = _Node("10.0.0.1")
        self.workers = [_Node("10.0.0.2"), _Node("10.0.0.3")]


def _deployment(target="all", completed=None):
    return mods.ModDeployment(
        mod_name="nccl",
        mod_path=Path("/mods/nccl"),
        target=target,
        completed_nodes=completed or [],
    )


# ── API parity with the real module ──────────────────────────────────────────


def test_the_mock_exposes_every_public_name_the_real_module_does():
    """The switch hands callers one module or the other; both must answer.

    ``routers/mods.py`` builds ``mods.ModDeployment`` and
    ``mods.ModOrchestrator`` through the switch. The mock defined neither, so
    every apply and rollback in simulation was a 500 — this is the test that
    would have said so.
    """
    # Both ``from spark_pulse.tools import mods`` and ``import
    # spark_pulse.tools.mods as real`` read the *attribute* on the tools
    # package, which under SIMULATION_MODE is the mock — the comparison would
    # be with itself. sys.modules holds the submodule, the way conftest.py
    # reaches the real modules it has to patch.
    import sys

    import spark_pulse.tools.mods  # noqa: F401

    real = sys.modules["spark_pulse.tools.mods"]

    def api(module):
        """The public names the module defines itself, not the ones it imports."""
        return {
            name
            for name, value in vars(module).items()
            if not name.startswith("_")
            and getattr(value, "__module__", None) == module.__name__
        }

    expected = api(real)

    assert expected == {
        "ModDeployment",
        "ModOrchestrator",
        "list_mods",
        "get_mod",
        "validate_mod_content",
    }
    assert {name for name in expected if not hasattr(mods, name)} == set()


# ── Canned data ──────────────────────────────────────────────────────────────


class TestListing:
    def test_every_simulated_mod_has_the_shape_the_page_reads(self):
        for mod in mods.list_mods():
            assert mod["id"]
            assert isinstance(mod["files"], list)
            assert mod["has_patches"] is any(f["kind"] == "patch" for f in mod["files"])

    def test_the_listing_is_a_copy_a_caller_cannot_corrupt(self):
        listed = mods.list_mods()
        listed.append({"id": "injected"})

        assert "injected" not in [m["id"] for m in mods.list_mods()]

    def test_a_known_mod_is_returned_with_its_script(self):
        mod = mods.get_mod("tuning-benchmark")

        assert mod["script"].startswith("#!/bin/bash")
        assert {"name": "hooks.py", "kind": "python"} in mod["files"]

    def test_an_unknown_mod_is_none(self):
        assert mods.get_mod("nope") is None


# ── Scenario-driven validation ───────────────────────────────────────────────


class TestValidateModContent:
    def test_an_ordinary_mod_is_healthy_and_silent(self):
        result = mods.validate_mod_content(Path("/mods/plain"))

        assert result.healthy is True
        assert result.warnings == []
        assert result.errors == []

    @pytest.mark.parametrize(
        ("name", "expected_error"),
        [
            ("dangerous-mod", "Dangerous pattern"),
            ("oversized-mod", "maximum size"),
            ("zipbomb-mod", "zip bomb"),
        ],
    )
    def test_the_name_selects_the_failure_being_simulated(self, name, expected_error):
        result = mods.validate_mod_content(Path("/mods") / name)

        assert result.healthy is False
        assert any(expected_error in e for e in result.errors)

    def test_a_networked_mod_is_allowed_but_warned_about(self):
        result = mods.validate_mod_content(Path("/mods/network-fetch"))

        assert result.healthy is True
        assert result.warnings == ["run.sh uses network access (curl/wget)"]

    def test_the_orchestrator_validates_through_the_same_helper(self):
        result = mods.ModOrchestrator().validate_mod(Path("/mods/dangerous"))

        assert result.healthy is False


# ── Scenario-driven orchestration ────────────────────────────────────────────


class TestOrchestrator:
    def test_it_takes_the_real_orchestrators_arguments(self):
        """A caller must be able to construct either class the same way."""
        orchestrator = mods.ModOrchestrator(ssh_client=object(), services=object())

        result = orchestrator.apply_mod_cluster(_deployment("head"), _Cluster())

        assert result.completed_nodes == ["10.0.0.1"]

    def test_success_completes_head_and_every_worker(self):
        result = mods.ModOrchestrator().apply_mod_cluster(_deployment(), _Cluster())

        assert result.completed_nodes == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
        assert result.failed_nodes == []

    def test_partial_failure_splits_the_nodes(self):
        orchestrator = mods.ModOrchestrator(scenario="partial_failure")

        result = orchestrator.apply_mod_cluster(_deployment(), _Cluster())

        assert result.completed_nodes == ["10.0.0.1"]
        assert result.failed_nodes == ["10.0.0.2", "10.0.0.3"]

    def test_all_fail_completes_nothing(self):
        orchestrator = mods.ModOrchestrator(scenario="all_fail")

        result = orchestrator.apply_mod_cluster(_deployment(), _Cluster())

        assert result.completed_nodes == []
        assert result.failed_nodes == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]

    def test_an_unknown_scenario_behaves_like_success(self):
        orchestrator = mods.ModOrchestrator(scenario="who-knows")

        result = orchestrator.apply_mod_cluster(_deployment("workers"), _Cluster())

        assert result.completed_nodes == ["10.0.0.2", "10.0.0.3"]

    def test_every_application_is_recorded(self):
        orchestrator = mods.ModOrchestrator()

        orchestrator.apply_mod_cluster(_deployment("head"), _Cluster())
        orchestrator.apply_mod_cluster(_deployment("workers"), _Cluster())

        assert [d.target for d in orchestrator.deployments] == ["head", "workers"]

    def test_the_record_of_applications_is_a_copy(self):
        orchestrator = mods.ModOrchestrator()
        orchestrator.apply_mod_cluster(_deployment("head"), _Cluster())

        orchestrator.deployments.clear()

        assert len(orchestrator.deployments) == 1

    def test_rollback_touches_only_the_nodes_that_took_the_mod(self):
        rolled_back = mods.ModOrchestrator().rollback_mod(
            _deployment(completed=["10.0.0.2"]), _Cluster()
        )

        assert rolled_back == ["10.0.0.2"]

    def test_rollback_stays_inside_the_target(self):
        rolled_back = mods.ModOrchestrator().rollback_mod(
            _deployment(target="workers", completed=["10.0.0.1", "10.0.0.3"]),
            _Cluster(),
        )

        assert rolled_back == ["10.0.0.3"]
