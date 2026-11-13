"""The run-backend seam (#160): deploy_dispatch calls a RunBackend, not the runtime.

Three things hold it. The Docker backend is a RunBackend. Every dispatcher call
lands on the backend ``backend_for`` answers, so swapping that answer is the
whole of adding a backend. And nothing outside the backend package calls
``native_runtime``'s lifecycle functions, which is read from the source because
the property is *which module a call goes to*, the same reason
``test_no_local_operations`` reads it.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from spark_pulse import tools
from spark_pulse.tools import backends
from spark_pulse.tools.backends import DockerBackend, RunBackend, backend_for

PACKAGE = Path(__file__).resolve().parent.parent / "spark_pulse"

#: native_runtime's lifecycle: what a backend does with a run.
LIFECYCLE = frozenset(
    {
        "plan",
        "realise",
        "start",
        "create_deployment",
        "status",
        "stop_deployment",
        "delete_deployment",
        "get_logs",
        "list_deployments",
    }
)

#: Where those may be named: the runtime's re-export, its simulation twin
#: (which re-exports it), and the Docker backend, which owns the lifecycle
#: since #163 and delegates to it through that re-export.
MAY_CALL_LIFECYCLE = {
    "tools/native_runtime.py",
    "mock/native_runtime.py",
    "tools/backends/docker/__init__.py",
    "tools/backends/docker/planning.py",
    "tools/backends/docker/images.py",
    "tools/backends/docker/lifecycle.py",
    "tools/backends/docker/state.py",
}


class TestTheDockerBackend:
    def test_it_is_a_run_backend(self):
        assert isinstance(DockerBackend(), RunBackend)

    def test_it_implements_every_method_with_the_protocols_parameters(self):
        for name in (
            "plan",
            "realise",
            "model_location",
            "start",
            "status",
            "stop",
            "delete",
            "logs",
            "list_runs",
            "diagnose",
        ):
            want = inspect.signature(getattr(RunBackend, name))
            got = inspect.signature(getattr(DockerBackend, name))
            assert list(got.parameters) == list(want.parameters), name

    @pytest.mark.parametrize(
        "target",
        [None, "some-id", {"id": "d1", "runtime": "native"}, {"port": 8000}],
    )
    def test_it_owns_every_run_today(self, target):
        assert backend_for(target).name == "docker"

    def test_it_reaches_the_runtime_through_the_switch(self):
        """A patch on tools.native_runtime still lands: the lookup is per call."""
        with patch.object(
            tools.native_runtime, "list_deployments", return_value=[{"id": "x"}]
        ):
            assert DockerBackend().list_runs() == [{"id": "x"}]


class _Recorder:
    """A RunBackend that records what it was asked."""

    name = "recorder"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _note(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def plan(self, recipe_id, **kwargs):
        self._note("plan", recipe_id, **kwargs)

        class _Plan:
            def to_dict(self):
                return {"recipe_id": recipe_id}

        return _Plan()

    def realise(self, intent, services=None):
        self._note("realise", intent)

    def model_location(self, intent, node, services, warnings):
        return "", ""

    def start(self, recipe_id, name, params, **kwargs):
        self._note("start", recipe_id, name, params, **kwargs)
        return {"id": "new"}

    def status(self, run_id):
        self._note("status", run_id)
        return {"id": run_id}

    def stop(self, run_id):
        self._note("stop", run_id)
        return {"id": run_id}

    def delete(self, run_id):
        self._note("delete", run_id)
        return True

    def logs(self, run_id, lines=200):
        self._note("logs", run_id, lines)
        return "tail"

    def list_runs(self):
        self._note("list_runs")
        return [{"id": "b", "created_at": "2"}, {"id": "a", "created_at": "1"}]

    def diagnose(self, node_id, label="", run=None, facts=None):
        self._note("diagnose", node_id, label)


@pytest.fixture
def recorder():
    fake = _Recorder()
    assert isinstance(fake, RunBackend)
    with patch.object(backends, "backend_for", lambda _target=None: fake):
        yield fake


class TestTheDispatcherGoesThroughTheBackend:
    def test_list(self, recorder):
        runs = tools.deploy_dispatch.list_deployments()
        assert [r["id"] for r in runs] == ["a", "b"]
        assert all(r["serves"] == "chat" for r in runs)
        assert [c[0] for c in recorder.calls] == ["list_runs"]

    def test_create(self, recorder):
        tools.deploy_dispatch.create_deployment("r", "n", {"port": 1}, nodes=["x"])
        name, args, kwargs = recorder.calls[0]
        assert (name, args) == ("start", ("r", "n", {"port": 1}))
        assert kwargs["nodes"] == ["x"]

    def test_plan_is_a_dict_and_solo_follows_the_nodes(self, recorder):
        assert tools.deploy_dispatch.plan_deployment("r") == {"recipe_id": "r"}
        tools.deploy_dispatch.plan_deployment("r", nodes=["a", "b"])
        assert recorder.calls[0][2]["solo"] is True
        assert recorder.calls[1][2]["solo"] is False

    def test_stop_delete_logs(self, recorder):
        tools.deploy_dispatch.stop_deployment("d1")
        tools.deploy_dispatch.delete_deployment("d1")
        assert tools.deploy_dispatch.get_logs("d1", 5) == "tail"
        assert [(c[0], c[1]) for c in recorder.calls] == [
            ("stop", ("d1",)),
            ("delete", ("d1",)),
            ("logs", ("d1", 5)),
        ]

    def test_status_only_for_a_run_on_record(self, recorder):
        assert tools.deploy_dispatch.get_deployment("nobody") is None
        assert recorder.calls == []


def _lifecycle_calls(path: Path) -> list[str]:
    """``native_runtime.<lifecycle>(...)`` calls and imports in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            owner_name = (
                owner.attr
                if isinstance(owner, ast.Attribute)
                else getattr(owner, "id", "")
            )
            if owner_name == "native_runtime" and node.func.attr in LIFECYCLE:
                found.append(f"{node.lineno}: native_runtime.{node.func.attr}(...)")
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "native_runtime"
        ):
            for alias in node.names:
                if alias.name in LIFECYCLE:
                    found.append(f"{node.lineno}: imports {alias.name}")
    return found


def test_nothing_outside_the_backends_calls_the_runtimes_lifecycle():
    offenders = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel in MAY_CALL_LIFECYCLE:
            continue
        calls = _lifecycle_calls(path)
        if calls:
            offenders[rel] = calls
    assert not offenders, (
        "These call native_runtime's lifecycle directly. Go through "
        f"tools.deploy_dispatch (or a RunBackend) instead: {offenders}"
    )


def test_the_ratchet_sees_a_call_when_there_is_one(tmp_path):
    """So an empty answer above means nothing found, not nothing looked at."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from spark_pulse import tools\n"
        "tools.native_runtime.list_deployments()\n"
        "from spark_pulse.tools.native_runtime import stop_deployment\n"
    )
    assert len(_lifecycle_calls(probe)) == 2


#: The runtime check sets and how they are chosen and run (#168). Only the
#: backend picks them; the doctor and the pre-flight go through ``diagnose``.
RUNTIME_CHECK_SETS = frozenset(
    {"DockerChecks", "PodmanChecks", "UnknownRuntimeChecks", "checks_for", "run_checks"}
)


def _runtime_check_uses(path: Path) -> list[str]:
    """Names of a check set used or imported in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in RUNTIME_CHECK_SETS:
            found.append(f"{node.lineno}: .{node.attr}")
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "runtime_checks"
        ):
            found.extend(
                f"{node.lineno}: imports {a.name}"
                for a in node.names
                if a.name in RUNTIME_CHECK_SETS
            )
    return found


def test_only_the_backends_choose_the_runtime_checks():
    offenders = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith("tools/backends/"):
            continue
        uses = _runtime_check_uses(path)
        if uses:
            offenders[rel] = uses
    assert not offenders, (
        "These pick or run a runtime check set directly. Ask the run backend "
        f"instead: backends.backend_for().diagnose(node_id, ...): {offenders}"
    )


def test_the_runtime_ratchet_sees_a_use_when_there_is_one(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from spark_pulse.tools.backends.runtime_checks import checks_for\n"
        "from spark_pulse.tools.backends import runtime_checks\n"
        "runtime_checks.PodmanChecks()\n"
    )
    assert len(_runtime_check_uses(probe)) == 2
