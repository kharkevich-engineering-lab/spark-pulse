"""The /api/launch-script endpoints, as the cluster page's analyser calls them.

These run against the simulated backend, which is the point: until the mock
module was given the real analysis functions, every one of these four endpoints
answered 500 in SIMULATION_MODE while passing in production.

Each endpoint takes a free-form body, so the interesting half is the refusals:
a missing path, a file that is not a launch script, a node count below one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.config import config
from spark_pulse.routers import launch_script as router

GOOD_SCRIPT = (
    "#!/bin/bash\n"
    "python -m vllm.entrypoints.openai.api_server \\\n"
    "  --model org/model \\\n"
    "  --tensor-parallel-size 4 \\\n"
    "  --distributed-executor-backend ray\n"
)

#: Nothing a launch script would ever contain — validation refuses it.
NOT_A_LAUNCH_SCRIPT = "#!/bin/bash\necho hello\n"


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "launch.sh"
    path.write_text(GOOD_SCRIPT, encoding="utf-8")
    return path


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A configured spark-vllm-docker checkout with one example script."""
    examples = tmp_path / "checkout" / "examples"
    examples.mkdir(parents=True)
    (examples / "qwen.sh").write_text(GOOD_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(
        type(config),
        "spark_vllm_path",
        property(lambda self: str(tmp_path / "checkout")),
    )
    return examples


@pytest.mark.parametrize("endpoint", ["resolve", "analyze", "validate", "patch"])
def test_every_endpoint_refuses_an_empty_path(client, endpoint):
    resp = client.post(f"/api/launch-script/{endpoint}", json={})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "path is required"


class TestResolve:
    def test_an_absolute_path_resolves_to_itself(self, client, script):
        resp = client.post("/api/launch-script/resolve", json={"path": str(script)})

        assert resp.status_code == 200
        assert resp.json() == {
            "path": str(script),
            "exists": True,
            "is_file": True,
        }

    def test_a_bare_name_resolves_inside_the_checkout(self, client, checkout):
        resp = client.post("/api/launch-script/resolve", json={"path": "qwen"})

        assert resp.status_code == 200
        assert resp.json()["path"] == str(checkout / "qwen.sh")

    def test_a_missing_script_is_404_not_500(self, client, tmp_path):
        resp = client.post(
            "/api/launch-script/resolve", json={"path": str(tmp_path / "nope.sh")}
        )

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_an_unexpected_failure_is_500(self, client, monkeypatch, script):
        monkeypatch.setattr(
            router.launch_script,
            "LaunchScriptManager",
            _raising(OSError("disk went away")),
        )

        resp = client.post("/api/launch-script/resolve", json={"path": str(script)})

        assert resp.status_code == 500
        assert resp.json()["detail"] == "disk went away"


class TestAnalyze:
    def test_it_reports_parallelism_backend_and_validation(self, client, script):
        resp = client.post("/api/launch-script/analyze", json={"path": str(script)})

        assert resp.status_code == 200
        body = resp.json()
        assert body["path"] == str(script)
        assert body["command_line"].startswith("python -m vllm.entrypoints")
        assert body["parallelism"] == {"tp": 4, "pp": 1, "dp": 1}
        assert body["backend"] == "ray"
        assert body["has_model_flag"] is True
        assert body["is_valid"] is True
        assert body["validation"] == {"healthy": True, "warnings": [], "errors": []}

    def test_a_script_without_a_model_flag_is_valid_but_warns(self, client, tmp_path):
        path = tmp_path / "no-model.sh"
        path.write_text("#!/bin/bash\nvllm serve --port 8000\n", encoding="utf-8")

        body = client.post(
            "/api/launch-script/analyze", json={"path": str(path)}
        ).json()

        assert body["is_valid"] is True
        assert body["has_model_flag"] is False
        assert body["validation"]["warnings"] == [
            "Launch script does not contain --model flag"
        ]

    def test_a_missing_file_analyses_as_invalid_rather_than_erroring(
        self, client, tmp_path
    ):
        # The analyser answers for a path the operator is still typing, so a
        # file that is not there is a validation error, not a 404 or a 500.
        body = client.post(
            "/api/launch-script/analyze", json={"path": str(tmp_path / "nope.sh")}
        ).json()

        assert body["is_valid"] is False
        assert body["command_line"] is None
        assert "not found" in body["validation"]["errors"][0]

    def test_an_unexpected_failure_is_500(self, client, monkeypatch, script):
        monkeypatch.setattr(
            router.launch_script,
            "analyze_launch_script",
            _raising(RuntimeError("boom")),
        )

        resp = client.post("/api/launch-script/analyze", json={"path": str(script)})

        assert resp.status_code == 500
        assert resp.json()["detail"] == "boom"


class TestValidate:
    def test_a_launch_script_is_healthy(self, client, script):
        resp = client.post("/api/launch-script/validate", json={"path": str(script)})

        assert resp.status_code == 200
        assert resp.json() == {"healthy": True, "warnings": [], "errors": []}

    def test_a_script_that_launches_nothing_is_unhealthy(self, client, tmp_path):
        path = tmp_path / "hello.sh"
        path.write_text(NOT_A_LAUNCH_SCRIPT, encoding="utf-8")

        body = client.post(
            "/api/launch-script/validate", json={"path": str(path)}
        ).json()

        assert body["healthy"] is False
        assert body["errors"] == [
            "Launch script does not appear to contain a python/vllm command"
        ]

    def test_an_unexpected_failure_is_500(self, client, monkeypatch, script):
        monkeypatch.setattr(
            router.launch_script,
            "validate_launch_script",
            _raising(RuntimeError("boom")),
        )

        resp = client.post("/api/launch-script/validate", json={"path": str(script)})

        assert resp.status_code == 500
        assert resp.json()["detail"] == "boom"


class TestPatch:
    def test_it_returns_one_script_per_node_rank(self, client, script):
        resp = client.post(
            "/api/launch-script/patch",
            json={
                "path": str(script),
                "total_nodes": 3,
                "master_addr": "10.0.0.1",
                "master_port": 29999,
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["original_script"] == str(script)
        assert body["total_nodes"] == 3
        assert body["master_addr"] == "10.0.0.1"
        assert body["master_port"] == 29999
        # JSON object keys are strings, one per rank.
        assert sorted(body["scripts"]) == ["0", "1", "2"]
        assert [Path(p).name for _, p in sorted(body["scripts"].items())] == [
            "node0.sh",
            "node1.sh",
            "node2.sh",
        ]

    def test_the_defaults_are_a_single_local_node(self, client, script):
        body = client.post(
            "/api/launch-script/patch", json={"path": str(script)}
        ).json()

        assert body["total_nodes"] == 1
        assert body["master_addr"] == "127.0.0.1"
        assert body["master_port"] == 29500
        assert list(body["scripts"]) == ["0"]

    def test_the_temporary_bundle_does_not_outlive_the_request(self, client, script):
        # The endpoint cleans up before returning, so the paths it reports are
        # a description of the patch set, not files to go and read.
        body = client.post(
            "/api/launch-script/patch", json={"path": str(script), "total_nodes": 2}
        ).json()

        assert [Path(p).exists() for p in body["scripts"].values()] == [False, False]

    def test_fewer_than_one_node_is_refused(self, client, script):
        resp = client.post(
            "/api/launch-script/patch", json={"path": str(script), "total_nodes": 0}
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "total_nodes must be >= 1"

    def test_a_script_that_fails_validation_is_refused(self, client, tmp_path):
        path = tmp_path / "hello.sh"
        path.write_text(NOT_A_LAUNCH_SCRIPT, encoding="utf-8")

        resp = client.post(
            "/api/launch-script/patch", json={"path": str(path), "total_nodes": 2}
        )

        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("Launch script validation failed:")

    def test_an_unexpected_failure_is_500(self, client, monkeypatch, script):
        monkeypatch.setattr(
            router.launch_script,
            "LaunchScriptManager",
            _raising(RuntimeError("boom")),
        )

        resp = client.post(
            "/api/launch-script/patch", json={"path": str(script), "total_nodes": 2}
        )

        assert resp.status_code == 500
        assert resp.json()["detail"] == "boom"


def _raising(error: Exception):
    """A stand-in that raises when the router calls it."""

    def raise_it(*_args, **_kwargs):
        raise error

    return raise_it
