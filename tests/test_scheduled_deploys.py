"""Deploying a recipe whose model has not been downloaded yet.

The behaviour under test is a chain: a create that cannot proceed says *which*
model is missing, the operator accepts the offer to fetch it, and the
deployment happens when the bytes land — without the browser being involved in
any of it.

Each link is tested where it can actually fail. The error shape is a contract
with the UI, so it is asserted as a shape. The trigger is asserted by driving
the download to completion and looking for the deployment, not by checking
that a hook was registered — a registered hook that never fires is exactly the
bug this would otherwise miss.
"""

from __future__ import annotations

import importlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.tools import scheduled_deploys

RECIPE = "bundled/qwen2.5-0.5b-instruct"

#: A model no simulated Spark has in its catalogue.
ABSENT = "nobody/never-downloaded"


@pytest.fixture
def client():
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _clean_deployments():
    """Undo the deployments these tests create.

    Simulation state outlives the test that made it — the container service is
    one in-memory instance for the whole process, and the database fixture
    cannot reach it. A deployment left behind holds its image, and the image
    tests later find that image "in use by running deployment(s)". The
    pre-flight router tests hit this first; the same rule applies here, and
    more so, because half of these tests exist to create a deployment.
    """
    from spark_pulse import tools

    before = {d["id"] for d in tools.deploy_dispatch.list_deployments()}
    yield
    # Every deploy dispatched to a thread has to have landed before the count
    # is taken, or it lands in the next test's world instead.
    for thread in threading.enumerate():
        if thread.name.startswith("scheduled-deploy-"):
            thread.join(timeout=5)
    for dep in tools.deploy_dispatch.list_deployments():
        if dep["id"] not in before:
            # Twice: the first stops it, the second drops the record.
            tools.deploy_dispatch.stop_deployment(dep["id"])
            tools.deploy_dispatch.delete_deployment(dep["id"])


@pytest.fixture(autouse=True)
def _clean_downloads():
    """Download state is process-global; it leaks into the next test.

    Including the simulated catalogue: a test that drives a download to
    completion leaves that model *present*, and the next test asking for a
    model nobody has would find it already there.
    """
    yield
    real_models = importlib.import_module("spark_pulse.tools.models")
    for module in (real_models, tools.models):
        for name in ("_jobs", "_notified", "_downloaded", "_cancelled", "_deleted"):
            state = getattr(module, name, None)
            if state is not None:
                state.clear()


# ── The error that starts it all ────────────────────────────────────────────


class TestTheMissingModelIsNamed:
    def test_a_create_says_which_model_is_missing(self, client):
        """Not just prose: the id the client needs to offer the download."""
        response = client.post(
            "/api/deployments",
            json={"recipe_id": RECIPE, "model": ABSENT, "skip_preflight": True},
        )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["missing_model"]["model"] == ABSENT
        assert detail["missing_model"]["recipe_id"] == RECIPE
        assert ABSENT in detail["message"]

    def test_another_planning_failure_is_still_a_plain_message(self, client):
        """Only the missing model is structured; nothing else changed shape."""
        response = client.post(
            "/api/deployments",
            json={"recipe_id": RECIPE, "engine": "not-an-engine"},
        )

        assert response.status_code in (400, 404)
        assert isinstance(response.json()["detail"], str)

    def test_the_preview_reports_it_before_the_deploy_does(self, client):
        """The plan permits a missing model, so it used to say nothing at all.

        An operator pressing Deploy found out from the 400. The preview runs
        first and now carries the answer, which is where they are already
        looking.
        """
        response = client.post(
            "/api/deployments/plan", json={"recipe_id": RECIPE, "model": ABSENT}
        )

        assert response.status_code == 200
        plan = response.json()
        assert plan["model_present"] is False
        assert any(ABSENT in w for w in plan["warnings"])

    def test_a_model_that_is_here_reads_as_present(self, client):
        response = client.post("/api/deployments/plan", json={"recipe_id": RECIPE})

        assert response.json()["model_present"] is True


# ── Accepting the offer ─────────────────────────────────────────────────────


class TestSchedulingTheDeploy:
    def test_it_starts_the_download_and_records_the_deployment(self, client):
        response = client.post(
            "/api/scheduled-deploys",
            json={"recipe_id": RECIPE, "name": "later", "model": ABSENT},
        )

        assert response.status_code == 200
        entry = response.json()
        assert entry["model"] == ABSENT
        assert entry["status"] == "waiting"
        assert entry["name"] == "later"
        # The job id travels with the answer so the caller can follow progress
        # without a second round trip.
        assert entry["download_job_id"]
        assert entry["download"]["model"] == ABSENT

    def test_the_recipe_supplies_the_model_when_the_request_does_not(self, client):
        """The planner resolves the model; the client should not have to."""
        response = client.post("/api/scheduled-deploys", json={"recipe_id": RECIPE})

        # This recipe's model *is* in the catalogue, so there is nothing to
        # wait for — and saying so is better than queueing a download of
        # something already here.
        assert response.status_code == 409
        assert "already here" in response.json()["detail"]["message"]

    def test_an_unknown_recipe_is_a_404(self, client):
        response = client.post("/api/scheduled-deploys", json={"recipe_id": "nope"})

        assert response.status_code == 404

    def test_it_is_listed_while_it_waits(self, client):
        client.post(
            "/api/scheduled-deploys", json={"recipe_id": RECIPE, "model": ABSENT}
        )

        listed = client.get("/api/scheduled-deploys?active_only=true").json()

        assert [e["model"] for e in listed] == [ABSENT]


# ── What happens when the bytes land ────────────────────────────────────────


class TestTheDownloadFinishing:
    def test_a_completed_download_creates_the_deployment(self, client):
        """The whole point, asserted end to end through the real trigger.

        The listener is the one the app registers at startup, and the job is
        driven to completion through the module's own publish path — so a hook
        that is registered but never reached fails this test.
        """
        tools.models.add_finish_listener(scheduled_deploys.on_download_finished)
        entry = client.post(
            "/api/scheduled-deploys",
            json={"recipe_id": RECIPE, "name": "arrives", "model": ABSENT},
        ).json()

        _complete(entry["download_job_id"], ABSENT)

        assert scheduled_deploys.get(entry["id"])["status"] == "done"
        names = [d["name"] for d in tools.deploy_dispatch.list_deployments()]
        assert "arrives" in names

    def test_a_failed_download_fails_the_schedule_with_the_reason(self):
        entry = scheduled_deploys.schedule(
            model=ABSENT, download_job_id="job1", request={"recipe_id": RECIPE}
        )

        scheduled_deploys.on_download_finished(
            {"id": "job1", "model": ABSENT, "status": "failed", "error": "no disk"}
        )

        settled = scheduled_deploys.get(entry["id"])
        assert settled["status"] == "failed"
        assert "no disk" in settled["error"]

    def test_the_model_arriving_by_another_route_still_deploys(self):
        """What the entry waits for is the model, not one particular job.

        An operator who cancels the offered download and fetches the model
        from the Models page instead has done the thing that was being waited
        for; refusing to notice would strand the deployment behind a job that
        no longer exists.
        """
        present = tools.models.list_models()[0]["id"]
        entry = scheduled_deploys.schedule(
            model=present,
            download_job_id="job1",
            request={"recipe_id": RECIPE, "name": "other-route", "model": present},
        )

        scheduled_deploys.on_download_finished(
            {"id": "some-other-job", "model": present, "status": "completed"}
        )

        assert scheduled_deploys.get(entry["id"])["status"] == "done"

    def test_another_jobs_failure_does_not_settle_this_one(self):
        """A failure says nothing about the download this is actually behind."""
        entry = scheduled_deploys.schedule(
            model=ABSENT, download_job_id="job1", request={"recipe_id": RECIPE}
        )

        scheduled_deploys.on_download_finished(
            {"id": "job2", "model": ABSENT, "status": "failed", "error": "boom"}
        )

        assert scheduled_deploys.get(entry["id"])["status"] == "waiting"

    def test_a_download_for_a_different_model_leaves_it_waiting(self):
        entry = scheduled_deploys.schedule(
            model=ABSENT, download_job_id="job1", request={"recipe_id": RECIPE}
        )

        scheduled_deploys.on_download_finished(
            {"id": "job2", "model": "somebody/else", "status": "completed"}
        )

        assert scheduled_deploys.get(entry["id"])["status"] == "waiting"

    def test_a_cancelled_schedule_does_not_deploy(self, client):
        """Cancelling has to win a race it can lose: the download may already
        be finishing when the operator presses the button."""
        entry = client.post(
            "/api/scheduled-deploys",
            json={"recipe_id": RECIPE, "name": "called-off", "model": ABSENT},
        ).json()
        client.delete(f"/api/scheduled-deploys/{entry['id']}")

        scheduled_deploys.on_download_finished(
            {"id": entry["download_job_id"], "model": ABSENT, "status": "completed"}
        )

        assert scheduled_deploys.get(entry["id"])["status"] == "cancelled"
        names = [d["name"] for d in tools.deploy_dispatch.list_deployments()]
        assert "called-off" not in names


# ── Calling it off ──────────────────────────────────────────────────────────


class TestCancelling:
    def test_cancelling_takes_the_download_with_it(self, client):
        entry = client.post(
            "/api/scheduled-deploys", json={"recipe_id": RECIPE, "model": ABSENT}
        ).json()

        client.delete(f"/api/scheduled-deploys/{entry['id']}")

        job = tools.models.get_download(entry["download_job_id"])
        assert job["status"] == "cancelled" or job.get("cancel_requested")

    def test_the_download_can_be_kept(self, client):
        """ "Do not deploy this" and "stop fetching 20 GB" are two wishes."""
        entry = client.post(
            "/api/scheduled-deploys", json={"recipe_id": RECIPE, "model": ABSENT}
        ).json()

        client.delete(f"/api/scheduled-deploys/{entry['id']}?cancel_download=false")

        job = tools.models.get_download(entry["download_job_id"])
        assert job["status"] != "cancelled"
        assert not job.get("cancel_requested")

    def test_one_cancellation_does_not_starve_the_other_deployment(self, client):
        """Two deployments behind one model is a normal thing to ask for.

        Both schedules share the one download job — that is the dedupe working
        — so cancelling the first must leave the second's bytes coming.
        """
        first = client.post(
            "/api/scheduled-deploys",
            json={"recipe_id": RECIPE, "name": "one", "model": ABSENT},
        ).json()
        second = client.post(
            "/api/scheduled-deploys",
            json={"recipe_id": RECIPE, "name": "two", "model": ABSENT},
        ).json()
        assert first["download_job_id"] == second["download_job_id"]

        client.delete(f"/api/scheduled-deploys/{first['id']}")

        job = tools.models.get_download(first["download_job_id"])
        assert job["status"] != "cancelled"
        assert not job.get("cancel_requested")

    def test_cancelling_an_unknown_schedule_is_a_404(self, client):
        assert client.delete("/api/scheduled-deploys/nope").status_code == 404


# ── Surviving a restart ─────────────────────────────────────────────────────


class TestReconcile:
    def test_a_model_that_arrived_while_it_was_down_still_deploys(self):
        """The hook is an optimisation; this is the guarantee."""
        present = tools.models.list_models()[0]["id"]
        entry = scheduled_deploys.schedule(
            model=present,
            download_job_id="job1",
            request={"recipe_id": RECIPE, "name": "recovered", "model": present},
        )

        assert scheduled_deploys.reconcile() == 1

        # Dispatched to a thread on purpose: creating a deployment pulls an
        # image, and doing that inline would hold up the control plane's
        # startup, which is where this runs.
        assert _settles(entry["id"]) == "done"
        names = [d["name"] for d in tools.deploy_dispatch.list_deployments()]
        assert "recovered" in names

    def test_a_download_that_died_with_the_process_is_reported(self):
        entry = scheduled_deploys.schedule(
            model=ABSENT, download_job_id="gone", request={"recipe_id": RECIPE}
        )

        assert scheduled_deploys.reconcile() == 1

        settled = scheduled_deploys.get(entry["id"])
        assert settled["status"] == "failed"
        assert "no longer running" in settled["error"]

    def test_an_interrupted_deploy_is_reported_not_retried(self):
        """Half a create may have started a container; doing it twice is worse."""
        entry = scheduled_deploys.schedule(
            model=ABSENT, download_job_id="job1", request={"recipe_id": RECIPE}
        )
        scheduled_deploys._finish(entry["id"], scheduled_deploys.STATUS_DEPLOYING)

        scheduled_deploys.reconcile()

        settled = scheduled_deploys.get(entry["id"])
        assert settled["status"] == "failed"
        assert "restarted" in settled["error"]

    def test_only_one_caller_can_claim_an_entry(self):
        """Two control planes on one database must not both deploy it.

        The read-then-write this replaced had a window in which both saw
        ``waiting`` — and the operator got two containers for one deployment
        they asked for once.
        """
        entry = scheduled_deploys.schedule(
            model=ABSENT, download_job_id="job1", request={"recipe_id": RECIPE}
        )

        assert scheduled_deploys._claim(entry["id"]) is True
        assert scheduled_deploys._claim(entry["id"]) is False

    def test_a_settled_entry_is_left_alone(self):
        entry = scheduled_deploys.schedule(
            model=ABSENT, download_job_id="job1", request={"recipe_id": RECIPE}
        )
        scheduled_deploys.cancel(entry["id"], cancel_download=False)

        assert scheduled_deploys.reconcile() == 0
        assert scheduled_deploys.get(entry["id"])["status"] == "cancelled"


# ── Firing once ─────────────────────────────────────────────────────────────


class TestTheHookFiresOnce:
    def test_two_publishes_of_one_terminal_state_call_the_hook_once(self):
        """A queued job cancelled through the API is settled twice: once by
        ``cancel_download`` and again by the thread that picks it up. A hook
        that starts a deployment must not run for both."""
        models = importlib.import_module("spark_pulse.tools.models")
        seen: list[dict] = []
        listener = seen.append
        models.add_finish_listener(listener)
        try:
            job = {"id": "dup", "model": "m", "status": "cancelled"}
            models._publish_job(models.EVENT_CANCELLED, job)
            models._publish_job(models.EVENT_CANCELLED, job)
        finally:
            models._finish_hooks.remove(listener)
            models._notified.discard("dup")

        assert len(seen) == 1

    def test_a_listener_that_raises_does_not_lose_the_job(self):
        models = importlib.import_module("spark_pulse.tools.models")

        def angry(_job):
            raise RuntimeError("boom")

        seen: list[dict] = []
        listener = seen.append
        models.add_finish_listener(angry)
        models.add_finish_listener(listener)
        try:
            models._publish_job(
                models.EVENT_COMPLETED,
                {"id": "raises", "model": "m", "status": "completed"},
            )
        finally:
            models._finish_hooks.remove(angry)
            models._finish_hooks.remove(listener)
            models._notified.discard("raises")

        assert len(seen) == 1


def _settles(entry_id: str, timeout: float = 5.0) -> str:
    """The entry's status once it has left the active states."""
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = str((scheduled_deploys.get(entry_id) or {}).get("status") or "")
        if status not in ("waiting", "deploying"):
            return status
        time.sleep(0.02)
    return status


def _complete(job_id: str, model: str) -> None:
    """Drive a simulated download to completion through its own publish path."""
    tools.models._record_downloaded(model, 1_000)
    job = tools.models._set_job(job_id, status="completed", bytes_done=1_000)
    tools.models._publish_job(tools.models.EVENT_COMPLETED, job)
