"""Tests for the /api/cache and /api/memory routers.

Both are thin, both were untested, and both are what the Monitoring and Cache
pages poll. The interesting parts are the shapes they wrap around the tools
(`{"entries": ...}`, `{"gpus": ...}`, `{"disks": ...}`), what the memory
endpoint now says about *every* node rather than about this one, and the two
failure codes the kill endpoint turns a node's answer into.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.mock import node_service as mock_node_service


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


# ── Cache ────────────────────────────────────────────────────────────────────


class TestCacheRouter:
    def test_the_listing_is_wrapped_in_entries(self, client, monkeypatch):
        entry = {"name": "HF Model Cache", "path": "/c/hf", "size_bytes": 7}
        monkeypatch.setattr(tools.cache, "list_cache", lambda: [entry])

        assert client.get("/api/cache").json() == {"entries": [entry]}

    def test_cleaning_reports_a_result_per_target(self, client, monkeypatch):
        asked: list[list[str]] = []
        monkeypatch.setattr(
            tools.cache,
            "clean_cache",
            lambda targets: asked.append(targets) or {t: "Cleaned" for t in targets},
        )

        response = client.post("/api/cache/clean", json={"targets": ["CCache"]})

        assert response.json() == {"results": {"CCache": "Cleaned"}}
        assert asked == [["CCache"]]

    def test_cleaning_nothing_deletes_nothing(self, client, monkeypatch):
        monkeypatch.setattr(
            tools.cache,
            "clean_cache",
            lambda targets: pytest.fail("must not clean without a target"),
        )

        assert client.post("/api/cache/clean", json={}).json() == {
            "error": "No targets specified"
        }
        assert client.post("/api/cache/clean", json={"targets": []}).json() == {
            "error": "No targets specified"
        }


# ── Memory ───────────────────────────────────────────────────────────────────


def _control(client) -> dict:
    """The control node's block. There is no flat copy of it any more."""
    body = client.get("/api/memory").json()
    return next(n for n in body["nodes"] if n["is_control_plane"])


def _deploy(client) -> dict:
    """Start one simulated deployment and return its record.

    Through the API and the simulated catalogue rather than a fabricated
    record: the point of these tests is that a *container* on a node holds a
    GPU process, and only a real create puts one there.
    """
    recipes = client.get("/api/recipes").json()
    recipe = next(r for r in recipes if r.get("id"))
    created = client.post(
        "/api/deployments",
        json={"recipe_id": recipe["id"], "name": "monitoring-test"},
    )
    assert created.status_code == 200, created.text
    return created.json()


class TestMemoryRouter:
    """Every node, asked the same way — including the one we run on.

    The page used to show whichever machine the control plane happened to be
    installed on, with nothing on it saying which. There was no node parameter
    anywhere in the chain, so on a four-node cluster it was one Spark out of
    four and an operator could not tell.
    """

    def test_the_answer_covers_every_registered_node(self, client):
        body = client.get("/api/memory").json()

        assert [n["name"] for n in body["nodes"]] == ["spark-01", "spark-02"]
        assert body["nodes"][0]["is_control_plane"] is True

    def test_there_is_one_shape_and_it_is_the_node_list(self, client):
        """No flat copy of the control node beside it: a payload that answers
        twice invites a page to read the wrong half and call it the cluster."""
        body = client.get("/api/memory").json()

        assert set(body) == {"nodes"}

    def test_a_node_that_cannot_be_asked_says_so_rather_than_vanishing(self, client):
        """A missing row and an idle machine look identical on a page."""
        mock_node_service.unreachable.add("10.0.0.11")
        try:
            body = client.get("/api/memory").json()
        finally:
            mock_node_service.unreachable.discard("10.0.0.11")

        peer = next(n for n in body["nodes"] if n["name"] == "spark-02")
        assert peer["reachable"] is False
        assert "10.0.0.11" in peer["error"]
        assert peer["gpu"] == []

    def test_a_gb10_reports_no_gpu_memory_rather_than_zero(self, client):
        """The pool is unified, so `nvidia-smi` prints `[N/A]`. A zero here
        would draw an empty bar for a full machine."""
        gpu = _control(client)["gpu"][0]

        assert gpu["name"] == "NVIDIA GB10"
        assert gpu["memory_supported"] is False
        assert gpu["memory_total"] == 0
        assert gpu["utilization"] == 12.0

    def test_host_memory_comes_back_in_megabytes(self, client):
        """The protocol is bytes; the page has always read `free -m`."""
        cpu = _control(client)["cpu"]

        assert cpu["total"] == 130_000_000_000 // (1024 * 1024)
        assert 0 < cpu["usage_percent"] < 100

    def test_disks_stay_in_bytes(self, client):
        disks = _control(client)["disk"]

        assert [d["mount"] for d in disks] == ["/"]
        assert disks[0]["total"] > 1_000_000_000

    def test_a_process_in_a_container_we_started_names_its_deployment(self, client):
        """Two halves of one question: the node can see the process, and only
        the control plane knows which containers are its own."""
        created = _deploy(client)

        processes = _control(client)["processes"]

        mine = [p for p in processes if p["deployment"] == created["id"]]
        assert mine, "the deployment's own container held no GPU process"
        assert mine[0]["is_tracked"] is True
        assert mine[0]["container_name"] == created["container_name"]

    def test_a_process_nothing_claims_is_reported_untracked(self, client):
        """The row an operator opens this page for."""
        processes = _control(client)["processes"]

        stray = [p for p in processes if not p["is_tracked"]]
        assert [p["process_name"] for p in stray] == ["python3"]
        assert stray[0]["deployment"] == ""


class TestKillGpuProcess:
    def test_a_process_in_our_own_container_is_ended_by_stopping_it(self, client):
        """Killing the process inside would leave the container holding its
        ports, and the runtime would restart it."""
        created = _deploy(client)
        tracked = next(
            p for p in _control(client)["processes"] if p["deployment"] == created["id"]
        )

        result = client.delete(f"/api/memory/processes/{tracked['pid']}").json()

        assert result["killed"] is True
        assert result["container"] == created["container_name"]
        assert tools.docker.get_container_by_deployment(created["id"]) is None

    def test_a_stray_process_is_signalled_on_its_own_node(self, client):
        """The button used to work on exactly one machine, because the only
        implementation was an `os.kill` in this process."""
        response = client.delete("/api/memory/processes/4242?node=10.0.0.11")

        assert response.json()["killed"] is True
        assert (4242, False) in mock_node_service.docker_for(
            mock_node_service.peer_node("10.0.0.11")
        ).terminated

    def test_a_process_that_is_gone_is_a_404(self, client):
        response = client.delete("/api/memory/processes/999999")

        assert response.status_code == 404
        assert response.json()["detail"] == "Process 999999 not found"

    def test_a_node_with_no_agent_is_reported_rather_than_raised(self, client):
        mock_node_service.unreachable.add("10.0.0.11")
        try:
            response = client.delete("/api/memory/processes/4242?node=10.0.0.11")
        finally:
            mock_node_service.unreachable.discard("10.0.0.11")

        assert response.status_code == 200
        assert response.json()["killed"] is False
        assert "10.0.0.11" in response.json()["error"]
