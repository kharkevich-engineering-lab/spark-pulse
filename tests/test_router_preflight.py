"""The pre-flight REST API, against the simulated cluster.

Two properties matter more than the payload shape. The endpoint takes the deploy
plan's own request body, so the UI can send one form to both and the two answers
are about the same deployment. And it *starts nothing*: a pre-flight that had
side effects would be a deploy with extra steps.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.mock import preflight as mock_preflight

RECIPE = "bundled/qwen2.5-0.5b-instruct"


@pytest.fixture
def client():
    return TestClient(create_app())


def post(client: TestClient, **body):
    body.setdefault("recipe_id", RECIPE)
    return client.post("/api/preflight/run", json=body)


class TestRun:
    def test_a_solo_recipe_comes_back_with_a_verdict_and_checks(self, client):
        response = post(client)
        assert response.status_code == 200
        report = response.json()
        assert report["verdict"] in ("ready", "slow", "blocked")
        assert report["summary"]
        assert report["checks"], "a run with no checks is not a pre-flight"
        assert report["counts"]["pass"] > 0

    def test_every_check_names_its_node(self, client):
        report = post(client).json()
        nodes = {node["label"] for node in report["nodes"]}
        assert nodes
        assert all(check["node"] in nodes for check in report["checks"])

    def test_every_non_passing_check_carries_a_remedy(self, client):
        report = post(client, nodes=["192.168.1.100", "10.0.0.11"]).json()
        bad = [c for c in report["checks"] if c["status"] != "pass"]
        assert bad, "the simulated peer holds no image, so something must warn"
        for check in bad:
            assert check["remedy"].strip(), check
            assert check["node"] in check["observed"], check

    def test_it_takes_the_same_body_as_the_deploy_plan(self, client):
        body = {
            "recipe_id": RECIPE,
            "engine": "vllm",
            "model": "Qwen/Qwen2.5-0.5B-Instruct",
            "params": {"port": 9123},
            "extra_args": ["--max-num-seqs", "4"],
            "nodes": [],
            "allow_missing_model": True,
        }
        planned = client.post("/api/deployments/plan", json=body)
        checked = client.post("/api/preflight/run", json=body)
        assert planned.status_code == 200
        assert checked.status_code == 200
        assert checked.json()["plan"]["port"] == planned.json()["port"]
        assert checked.json()["plan"]["image_ref"] == planned.json()["image_ref"]

    def test_a_recipe_that_cannot_be_planned_is_a_400(self, client):
        response = post(client, recipe_id="does/not-exist")
        assert response.status_code == 400
        assert "does/not-exist" in response.json()["detail"]

    def test_a_node_that_is_missing_something_is_still_a_200(self, client):
        """ "This node has no GPU" is an answer, not a failed request."""
        mock_preflight.UNREACHABLE.add("10.0.0.11")
        response = post(client, nodes=["192.168.1.100", "10.0.0.11"])
        assert response.status_code == 200
        report = response.json()
        assert report["verdict"] == "blocked"
        assert report["can_proceed"] is False
        assert report["blocking"]
        assert report["blocking"][0]["node"] == "spark-02"
        assert report["blocking"][0]["remedy"]

    def test_it_starts_nothing(self, client):
        before = client.get("/api/deployments").json()
        post(client, nodes=["192.168.1.100", "10.0.0.11"])
        assert client.get("/api/deployments").json() == before

    def test_the_verdict_separates_blocked_from_slow_over_the_wire(self, client):
        slow = post(client, nodes=["192.168.1.100", "10.0.0.11"]).json()
        assert slow["verdict"] == "slow"
        assert slow["can_proceed"] is True
        assert slow["estimated_transfer_bytes"] > 0

        mock_preflight.UNREACHABLE.add("10.0.0.11")
        blocked = post(client, nodes=["192.168.1.100", "10.0.0.11"]).json()
        assert blocked["verdict"] == "blocked"
        assert blocked["can_proceed"] is False

    def test_the_report_splits_what_blocks_from_what_merely_delays(self, client):
        report = post(client, nodes=["192.168.1.100", "10.0.0.11"]).json()
        assert not report["blocking"]
        assert report["delaying"]
        ids = {c["id"] for c in report["delaying"]}
        assert "image" in ids
        assert all(c["status"] == "warn" for c in report["delaying"])
        assert all(c["status"] == "warn" for c in report["advisories"])

    def test_the_two_node_report_covers_both_nodes(self, client):
        report = post(client, nodes=["192.168.1.100", "10.0.0.11"]).json()
        assert [n["label"] for n in report["nodes"]] == ["spark-01", "spark-02"]
        assert {c["node"] for c in report["checks"]} == {"spark-01", "spark-02"}
