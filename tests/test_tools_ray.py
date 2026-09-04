"""Tests for RayManager.

Every assertion here is about *which node* the command ran on, because that is
what was broken: ``ensure_ray_worker`` took a worker IP, used it to build the
``--address`` flag, and then execed against the control node's own Docker
daemon, because the container service's host argument defaulted to empty. A
worker's Ray process was started on the head, every time.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from spark_pulse.tools.docker import ExecResult
from spark_pulse.tools.node_service import Node
from spark_pulse.tools.ray import RayManager

HEAD = "10.0.0.1"
WORKER = "10.0.0.2"


class RecordingServices:
    """A node resolver that hands out one recording service per node."""

    def __init__(self, answers=None):
        self.calls: list[tuple[str, str, list[str]]] = []
        self._answers = answers or (lambda argv: ExecResult(0, "Cluster is ready. OK"))
        self._services: dict[str, MagicMock] = {}

    def __call__(self, node: Node) -> MagicMock:
        key = node.address or node.id
        service = self._services.get(key)
        if service is None:
            service = MagicMock(name=f"NodeService({key})")

            def _exec(container, command, detach=False, timeout=None, _key=key):
                self.calls.append((_key, container, list(command)))
                return self._answers(list(command))

            service.exec_in_container.side_effect = _exec
            self._services[key] = service
        return service

    def nodes_touched(self) -> list[str]:
        seen: list[str] = []
        for node, _, _ in self.calls:
            if node not in seen:
                seen.append(node)
        return seen


def _never_ready(argv):
    """A node whose ``ray status`` says nothing useful."""
    if argv[:2] == ["ray", "status"]:
        return ExecResult(0, "not ready yet")
    return ExecResult(0, "")


class TestRayRunsOnTheNodeItNames:
    """The regression the empty host hid."""

    def test_the_worker_is_started_on_the_worker(self):
        services = RecordingServices()
        manager = RayManager(services)

        assert manager.ensure_ray_worker("c-worker-0", WORKER, HEAD) is True

        assert services.nodes_touched() == [WORKER]
        assert HEAD not in services.nodes_touched()

    def test_the_head_is_started_on_the_head(self):
        services = RecordingServices()
        manager = RayManager(services)

        assert manager.ensure_ray_head("c-head", HEAD) is True

        assert services.nodes_touched() == [HEAD]

    def test_a_worker_that_needs_starting_still_only_touches_that_worker(self):
        answers = iter(
            [
                ExecResult(1, "", "Ray not started"),  # status probe
                ExecResult(0, ""),  # ray start
                ExecResult(0, "Cluster is ready. OK"),  # readiness poll
            ]
        )
        services = RecordingServices(answers=lambda _argv: next(answers))
        manager = RayManager(services)

        assert manager.ensure_ray_worker("c-worker-0", WORKER, HEAD) is True

        assert services.nodes_touched() == [WORKER]
        start = next(
            argv for _, _, argv in services.calls if argv[:2] == ["ray", "start"]
        )
        assert "--address" in start
        assert f"{HEAD}:29501" in start
        assert start[start.index("--node-ip-address") + 1] == WORKER

    def test_the_head_start_command_carries_the_head_address(self):
        answers = iter(
            [
                ExecResult(1, "", "Ray not started"),
                ExecResult(0, ""),
                ExecResult(0, "Cluster is ready. OK"),
            ]
        )
        services = RecordingServices(answers=lambda _argv: next(answers))
        manager = RayManager(services)

        manager.ensure_ray_head("c-head", HEAD, port=6379)

        start = next(
            argv for _, _, argv in services.calls if argv[:2] == ["ray", "start"]
        )
        assert "--head" in start
        assert "--port=6379" in start
        assert start[start.index("--node-ip-address") + 1] == HEAD


class TestRayStatus:
    """Status and polling name their node like everything else."""

    def test_get_ray_status_returns_the_output_of_that_node(self):
        services = RecordingServices()
        manager = RayManager(services)

        status = manager.get_ray_status("c-head", HEAD)

        assert "ready" in status.lower()
        assert services.nodes_touched() == [HEAD]

    def test_get_ray_status_surfaces_the_error(self):
        services = RecordingServices(
            answers=lambda _argv: ExecResult(1, "", "Ray not started")
        )
        manager = RayManager(services)

        assert "not started" in manager.get_ray_status("c-head", HEAD)

    def test_wait_for_cluster_ready_times_out_on_that_node(self):
        services = RecordingServices(answers=_never_ready)
        manager = RayManager(services)

        assert (
            manager.wait_for_cluster_ready("c-head", HEAD, timeout=1, poll_interval=0.1)
            is False
        )
        assert services.nodes_touched() == [HEAD]

    def test_a_start_failure_is_reported_rather_than_polled(self):
        services = RecordingServices(
            answers=lambda argv: (
                ExecResult(1, "", "no such container")
                if argv[:2] == ["ray", "status"]
                else ExecResult(1, "", "ray start blew up")
            )
        )
        manager = RayManager(services)

        assert manager.ensure_ray_head("c-head", HEAD) is False

    @pytest.mark.parametrize("method", ["get_ray_status", "wait_for_cluster_ready"])
    def test_no_method_can_be_called_without_naming_a_node(self, method):
        """There is no node-less overload to fall back onto."""
        manager = RayManager(RecordingServices())
        with pytest.raises(TypeError):
            getattr(manager, method)("c-head")
