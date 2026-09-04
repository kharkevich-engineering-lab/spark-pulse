"""The simulated launch-script tools.

Analysing a launch script is pure work on a file the operator supplies, so the
mock re-exports the real implementation rather than guessing at answers — these
tests pin that delegation (the module used to define a scenario simulator that
answered by filename and omitted half the API, which left every
``/api/launch-script`` endpoint returning 500 under SIMULATION_MODE).

Only distribution reaches other machines, so only the distributor is simulated,
and what is asserted about it is that it keeps the real orchestration: same
ranks, same per-node failure handling, no SSH and no Docker.
"""

from __future__ import annotations

import importlib
import tempfile
import types
from pathlib import Path

import pytest

from spark_pulse.mock import launch_script as mock_ls

real_ls = importlib.import_module("spark_pulse.tools.launch_script")


def _api(module: types.ModuleType) -> set[str]:
    """The public names a module owns: what it defines, plus its own constants.

    Names it merely imported (``Path``, ``re``, helper modules) are not part of
    anyone's contract, so they are left out.
    """
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and not isinstance(value, types.ModuleType)
        and getattr(value, "__module__", module.__name__) == module.__name__
    }


def _defined_in(module: types.ModuleType) -> set[str]:
    """The public functions and classes this module implements itself."""
    return {
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and getattr(value, "__module__", None) == module.__name__
    }


def node(ip: str, role: str, container: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(ip=ip, role=role, container_name=container)


@pytest.fixture
def bundle():
    """A three-node bundle of patched scripts."""
    tmp = tempfile.TemporaryDirectory(prefix="spark-pulse-test-scripts-")
    scripts = {}
    for rank in range(3):
        path = Path(tmp.name) / f"node{rank}.sh"
        path.write_text(f"#!/bin/bash\npython serve --node-rank {rank}\n")
        scripts[rank] = path
    made = real_ls.PatchedScriptBundle(
        temp_dir=tmp,
        scripts=scripts,
        original_script=Path(tmp.name) / "original.sh",
        total_nodes=3,
    )
    yield made
    made.cleanup()


@pytest.fixture
def cluster():
    """A head plus two workers, the shape the real distributor walks."""
    return types.SimpleNamespace(
        head=node("127.0.0.1", "head", "vllm-head"),
        workers=[
            node("10.0.0.2", "worker", "vllm-w1"),
            node("10.0.0.3", "worker", "vllm-w2"),
        ],
    )


class TestContract:
    """The mock has to offer the whole real API — the router calls all of it."""

    def test_the_mock_offers_every_name_the_real_module_does(self):
        missing = {n for n in _api(real_ls) if not hasattr(mock_ls, n)}
        assert missing == set()

    @pytest.mark.parametrize(
        "name",
        [
            "ValidationResult",
            "LaunchScriptInfo",
            "PatchedScriptBundle",
            "LaunchScriptManager",
            "analyze_launch_script",
            "validate_launch_script",
            "validate_mod_content",
        ],
    )
    def test_analysis_is_the_real_implementation_not_a_lookalike(self, name):
        # Reading, validating and patching a script touches nothing but the
        # filesystem, so simulation must answer exactly as production does for
        # the same file rather than by scenario name.
        assert getattr(mock_ls, name) is getattr(real_ls, name)

    def test_only_the_distributor_is_simulated(self):
        assert _defined_in(mock_ls) == {"LaunchScriptDistributor"}
        assert issubclass(
            mock_ls.LaunchScriptDistributor, real_ls.LaunchScriptDistributor
        )


class TestDistributor:
    def test_constructing_it_never_reaches_docker(self, monkeypatch):
        # The real distributor builds NodeServices, which talks to Docker.
        node_service = importlib.import_module("spark_pulse.tools.node_service")
        monkeypatch.setattr(
            node_service,
            "NodeServices",
            lambda *a, **k: pytest.fail("simulation must not build NodeServices"),
        )

        assert mock_ls.LaunchScriptDistributor().deployments == []

    def test_a_deployment_is_recorded_rather_than_copied(self, tmp_path):
        distributor = mock_ls.LaunchScriptDistributor()
        script = tmp_path / "node1.sh"
        script.write_text("#!/bin/bash\n")

        # No SSH client: the real distributor would refuse a remote node.
        distributor.deploy_to_node(
            node=node("10.0.0.2", "worker", "vllm-w1"),
            script=script,
            container_name="vllm-w1",
        )

        assert distributor.deployments == [
            {
                "node_ip": "10.0.0.2",
                "node_role": "worker",
                "container": "vllm-w1",
                "script": str(script),
            }
        ]

    def test_the_cluster_walk_keeps_the_real_rank_mapping(self, bundle, cluster):
        distributor = mock_ls.LaunchScriptDistributor()

        results = distributor.deploy_to_cluster(cluster, bundle)

        assert results == {0: True, 1: True, 2: True}
        assert [d["node_ip"] for d in distributor.deployments] == [
            "127.0.0.1",
            "10.0.0.2",
            "10.0.0.3",
        ]
        # Rank 0 is the head, and each node gets its own patched script.
        assert [d["script"] for d in distributor.deployments] == [
            str(bundle.scripts[0]),
            str(bundle.scripts[1]),
            str(bundle.scripts[2]),
        ]
        assert [d["container"] for d in distributor.deployments] == [
            "vllm-head",
            "vllm-w1",
            "vllm-w2",
        ]

    def test_a_rank_with_no_patched_script_is_skipped(self, bundle, cluster):
        del bundle.scripts[2]
        distributor = mock_ls.LaunchScriptDistributor()

        results = distributor.deploy_to_cluster(cluster, bundle)

        assert results == {0: True, 1: True}
        assert len(distributor.deployments) == 2

    def test_one_failing_node_is_reported_without_stopping_the_rest(
        self, bundle, cluster
    ):
        distributor = mock_ls.LaunchScriptDistributor()
        deploy = distributor.deploy_to_node

        def fail_on_the_first_worker(node, script, container_name):
            if container_name == "vllm-w1":
                raise RuntimeError("node unreachable")
            deploy(node=node, script=script, container_name=container_name)

        distributor.deploy_to_node = fail_on_the_first_worker

        results = distributor.deploy_to_cluster(cluster, bundle)

        assert results == {0: True, 1: False, 2: True}
        assert [d["node_ip"] for d in distributor.deployments] == [
            "127.0.0.1",
            "10.0.0.3",
        ]
