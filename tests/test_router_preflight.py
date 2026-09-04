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

#: A create that should reach the runtime. The simulated catalogue holds every
#: bundled recipe's model, so nothing here is refused for a missing download.
CREATE = {"recipe_id": RECIPE}

#: A model no simulated Spark has, for the one test that wants an unresolvable
#: plan.
ABSENT_MODEL = "nobody/never-downloaded"


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


class TestTheDeployGate:
    """A create runs the pre-flight and refuses a blocked verdict.

    The point is that every blocking condition is one the deploy would hit
    anyway — minutes later, after a pull, with a worse message. Refusing early
    is the whole reason the checks exist.

    Every create is native, so the gate runs on every create that does not
    ask to skip it.
    """

    @pytest.fixture(autouse=True)
    def clean_up_deployments(self, client):
        before = {d["id"] for d in client.get("/api/deployments").json()}
        yield
        # A deployment left behind holds its image, and the image tests then
        # find it in use. Simulation state outlives the test that made it, so
        # a create here has to be undone here.
        for dep in client.get("/api/deployments").json():
            if dep["id"] not in before:
                client.delete(f"/api/deployments/{dep['id']}")
                client.delete(f"/api/deployments/{dep['id']}")

    def test_a_create_goes_ahead_when_nothing_blocks(self, client):
        response = client.post("/api/deployments", json=CREATE)
        assert response.status_code == 200, response.text
        assert response.json().get("id")

    def test_a_blocked_verdict_refuses_the_create_and_returns_the_report(
        self, client, monkeypatch
    ):
        blocked = {
            "verdict": mock_preflight.VERDICT_BLOCKED,
            "can_proceed": False,
            "summary": "spark-01 cannot run this deployment",
            "checks": [],
            "blocking": [{"id": "docker", "node": "spark-01"}],
        }
        monkeypatch.setattr(
            mock_preflight, "run", lambda *a, **k: blocked, raising=False
        )
        response = client.post("/api/deployments", json=CREATE)
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["message"] == "spark-01 cannot run this deployment"
        # The operator needs the checks, not just the word "blocked".
        assert detail["preflight"]["blocking"]

    def test_skip_preflight_deploys_anyway(self, client, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("skip_preflight must not run the checks at all")

        monkeypatch.setattr(mock_preflight, "run", _boom, raising=False)
        response = client.post(
            "/api/deployments", json={**CREATE, "skip_preflight": True}
        )
        assert response.status_code == 200, response.text

    def test_a_plan_the_pre_flight_cannot_resolve_leaves_the_create_alone(self, client):
        # A model that is not downloaded makes the pre-flight's own planning
        # raise before a single check runs. The create must then fail on the
        # real error rather than on a second, vaguer one from the checker.
        response = client.post(
            "/api/deployments", json={"recipe_id": RECIPE, "model": ABSENT_MODEL}
        )
        assert response.status_code == 400
        assert "not in the local catalogue" in response.json()["detail"]

    def test_a_pre_flight_that_itself_breaks_does_not_block_the_deploy(
        self, client, monkeypatch
    ):
        # A checker that raises must not become a new way for a deploy to fail.
        def _boom(*_a, **_k):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(mock_preflight, "run", _boom, raising=False)
        response = client.post("/api/deployments", json=CREATE)
        assert response.status_code == 200, response.text

    def test_a_create_that_proceeds_carries_the_advisories(self, client):
        body = client.post("/api/deployments", json=CREATE).json()
        # "the image is not on rank 1 yet" is what explains the first four
        # minutes of apparent silence after a deploy starts.
        assert "preflight" in body
        assert body["preflight"]["verdict"] in ("ready", "slow")
