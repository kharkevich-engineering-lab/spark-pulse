"""Tests for the engine image catalogue, pull jobs, deletion and sync.

The catalogue joins the engine registry against the Docker daemon, so both
sides are supplied here: bundled engine specs (offline) and the mock container
service the rest of the suite uses.
"""

from __future__ import annotations

import ast
import importlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# pytest-env forces SIMULATION_MODE=1, so ``from spark_pulse.tools import
# images`` hands back the mock. Resolve the real module — that is what this
# file tests.
images = importlib.import_module("spark_pulse.tools.images")

from spark_pulse.config import config  # noqa: E402
from spark_pulse.engines import EngineRegistry, reset_registry  # noqa: E402
from spark_pulse.mock.docker import (  # noqa: E402
    MockDockerClient,
    MockDockerService,
)
from spark_pulse.mock import registry as mock_registry  # noqa: E402
from spark_pulse.mock import node_service as mock_node_service  # noqa: E402
from spark_pulse.mock.node_service import NodeServices  # noqa: E402

VLLM_REPO = "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm"
VLLM_REF = f"{VLLM_REPO}:0.1.0"


@dataclass
class _Cluster:
    """The simulated other machines, and this machine's own registry.

    Each node has its own container service, so "n1 pulled and n2 did not" is
    a fact about two different stores rather than a filter over one command
    log. That distinction is why the old shared-daemon simulation could not
    have caught a distribution bug: every node's answer came from the same
    place.
    """

    services: Any
    registry: Any
    pulls: dict[str, list[str]] = field(default_factory=dict)

    def docker(self, host: str) -> Any:
        """The simulated Docker belonging to one node, recording its pulls."""
        docker = mock_node_service.docker_for(mock_node_service.peer_node(host))
        if host not in self.pulls:
            self.pulls[host] = []
            original = docker.pull_image

            def _record(ref, *args, **kwargs):
                self.pulls[host].append(ref)
                return original(ref, *args, **kwargs)

            docker.pull_image = _record
        return docker

    def fail(self, host: str) -> None:
        """Make that node unreachable, as a node with no agent would be."""

        def _unreachable(*_args, **_kwargs):
            raise RuntimeError(f"node {host} is unreachable (not_connected)")

        docker = self.docker(host)
        docker.image_info = _unreachable
        docker.pull_image = _unreachable

    def pulled(self, host: str) -> list[str]:
        """Every image reference this node was asked to pull."""
        self.docker(host)
        return list(self.pulls.get(host, []))


@pytest.fixture
def nodes():
    """One simulated container service per node, plus the local registry."""
    mock_node_service.reset()
    cluster = _Cluster(None, mock_registry.default_registry())
    cluster.services = NodeServices(
        resolver=lambda node, **_kwargs: cluster.docker(node.address or node.id)
    )
    try:
        yield cluster
    finally:
        mock_node_service.reset()


@pytest.fixture
def registry(tmp_path):
    """Bundled engine specs only — no index fetching, no network."""
    reset_registry()
    with patch.object(type(config), "engine_indexes", property(lambda self: [])):
        instance = EngineRegistry(cache_dir=tmp_path / "engine-cache")
        with patch("spark_pulse.tools.images.get_registry", return_value=instance):
            yield instance
    reset_registry()


@pytest.fixture
def docker():
    return MockDockerService(MockDockerClient())


@pytest.fixture
def catalogue(registry, docker):
    """The catalogue wired to the bundled specs and a fresh simulated host."""
    images._jobs.clear()
    images._cancelled.clear()
    with patch.object(images, "_docker", return_value=docker):
        yield docker
    images._jobs.clear()
    images._cancelled.clear()


# ── Catalogue ────────────────────────────────────────────────────────────────


class TestCatalogue:
    def test_lists_one_entry_per_engine_spec(self, catalogue):
        entries = images.list_images()

        engines = {e["engine"] for e in entries}
        assert {"vllm", "sglang"} <= engines
        entry = next(e for e in entries if e["ref"] == VLLM_REF)
        assert entry["repository"] == VLLM_REPO
        assert entry["tag"] == "0.1.0"
        assert entry["variant"] == "default"
        assert entry["engine_key"] == "vllm/default"

    def test_reports_a_present_image_with_its_size(self, catalogue):
        entry = images.get_image(VLLM_REF)

        assert entry is not None
        assert entry["present"] is True
        assert entry["size_bytes"] == 26_843_545_600
        assert entry["image_id"].startswith("sha256:")

    def test_reports_an_absent_image(self, catalogue):
        catalogue.client.images.remove(VLLM_REF)

        entry = images.get_image(VLLM_REF)

        assert entry["present"] is False
        assert entry["size_bytes"] == 0
        assert entry["update_available"] is True

    def test_carries_the_engine_legacy_tags(self, catalogue):
        """v1 recipes name ``vllm-node``; the catalogue says which image that is."""
        entry = images.get_image(VLLM_REF)
        assert "vllm-node" in entry["legacy_tags"]


class TestDigestDrift:
    """A republished version keeps its tag but changes its digest."""

    def _with_advertised_digest(self, registry, digest):
        spec = registry.get("vllm", "default")
        return patch.object(spec, "digest", digest)

    def test_drift_when_the_local_digest_differs(self, catalogue, registry):
        local = images.local_digest(catalogue.image_info(VLLM_REF), VLLM_REPO)
        with self._with_advertised_digest(registry, "sha256:" + "ff" * 32):
            entry = next(
                e for e in images.list_images() if e["engine_key"] == "vllm/default"
            )

        assert entry["local_digest"] == local
        assert entry["index_digest"] == "sha256:" + "ff" * 32
        assert entry["digest_drift"] is True
        assert entry["update_available"] is True

    def test_no_drift_when_the_digests_agree(self, catalogue, registry):
        local = images.local_digest(catalogue.image_info(VLLM_REF), VLLM_REPO)
        with self._with_advertised_digest(registry, local):
            entry = next(
                e for e in images.list_images() if e["engine_key"] == "vllm/default"
            )

        assert entry["digest_drift"] is False
        assert entry["update_available"] is False

    def test_a_stale_tag_is_not_an_update_when_the_content_is_here(
        self, catalogue, registry
    ):
        """Pulling by digest leaves the tag pointing at older content.

        The advertised image is on the host, so a deploy downloads nothing.
        Calling that an available update contradicts the two identical digests
        shown next to it; the stale tag is reported separately.
        """
        local = images.local_digest(catalogue.image_info(VLLM_REF), VLLM_REPO)
        with (
            self._with_advertised_digest(registry, local),
            patch.object(images, "local_digest", side_effect=lambda info, repo: local),
        ):
            entry = next(
                e for e in images.list_images() if e["engine_key"] == "vllm/default"
            )

        assert entry["local_digest"] == entry["index_digest"]
        assert entry["digest_drift"] is False
        assert entry["update_available"] is False

    def test_no_drift_claimed_when_the_index_advertises_nothing(self, catalogue):
        """Bundled specs carry no digest — absence of data is not drift."""
        entry = images.get_image(VLLM_REF)
        assert entry["index_digest"] == ""
        assert entry["digest_drift"] is False

    def test_no_drift_claimed_for_an_image_that_is_not_here(self, catalogue, registry):
        catalogue.client.images.remove(VLLM_REF)
        with self._with_advertised_digest(registry, "sha256:" + "ff" * 32):
            entry = next(
                e for e in images.list_images() if e["engine_key"] == "vllm/default"
            )

        assert entry["digest_drift"] is False
        assert entry["update_available"] is True


# ── Pull jobs ────────────────────────────────────────────────────────────────


def _wait(job_id, states, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = images.get_pull(job_id)
        if job and job["status"] in states:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {states}")


class TestPullJobs:
    def test_a_pull_runs_to_completion(self, catalogue):
        job = images.start_pull("ghcr.io/example/engine:2.0")
        assert job["status"] == "queued"

        done = _wait(job["id"], ("completed",))

        assert done["percent"] == 100.0
        assert done["bytes_total"] > 0
        assert done["image_id"]
        assert catalogue.image_exists("ghcr.io/example/engine:2.0")

    def test_pull_progress_is_aggregated_not_per_layer(self, catalogue):
        job = images.start_pull("ghcr.io/example/engine:2.0")
        _wait(job["id"], ("completed",))

        # The mock streams four chunks per layer over three layers; a
        # per-chunk job update would have reported a shrinking total.
        final = images.get_pull(job["id"])
        assert final["layers"] == 3
        assert final["bytes_done"] == final["bytes_total"]

    def test_a_second_pull_of_the_same_ref_joins_the_first(self, catalogue):
        first = images.start_pull("ghcr.io/example/engine:2.0")
        second = images.start_pull("ghcr.io/example/engine:2.0")

        assert second["id"] == first["id"]

    def test_a_failed_pull_records_the_error(self, catalogue):
        with patch.object(
            catalogue, "pull_image", side_effect=RuntimeError("registry down")
        ):
            job = images.start_pull("ghcr.io/example/broken:1")
            failed = _wait(job["id"], ("failed",))

        assert "registry down" in failed["error"]

    def test_cancelling_a_queued_pull(self, catalogue):
        job = {"id": "queued-job", "ref": "x", "status": "queued", "created_at": "now"}
        images._jobs["queued-job"] = job

        cancelled = images.cancel_pull("queued-job")

        assert cancelled["status"] == "cancelled"

    def test_cancelling_a_running_pull_stops_it(self, catalogue):
        job = images.start_pull("ghcr.io/example/engine:2.0")
        _wait(job["id"], ("running", "completed"))
        images.cancel_pull(job["id"])

        final = _wait(job["id"], ("cancelled", "completed"))
        # Either it was cancelled mid-stream or it had already finished; a
        # cancelled job must never be left claiming to run.
        assert final["status"] in ("cancelled", "completed")

    def test_unknown_job_is_none(self, catalogue):
        assert images.get_pull("nope") is None
        assert images.cancel_pull("nope") is None

    def test_clear_finished_pulls(self, catalogue):
        job = images.start_pull("ghcr.io/example/engine:2.0")
        _wait(job["id"], ("completed",))

        assert images.clear_finished_pulls() == 1
        assert images.list_pulls() == []


# ── Deletion ─────────────────────────────────────────────────────────────────


class TestDeletion:
    def test_deletes_a_local_image(self, catalogue):
        result = images.delete_image(VLLM_REF)

        assert result["deleted"] == VLLM_REF
        assert result["freed_bytes"] == 26_843_545_600
        assert catalogue.image_exists(VLLM_REF) is False

    def test_refuses_an_image_a_running_deployment_uses(self, catalogue):
        with patch.object(
            images,
            "images_in_use",
            return_value={VLLM_REF: ["dep-1"]},
        ):
            with pytest.raises(ValueError) as exc:
                images.delete_image(VLLM_REF)

        assert "in use" in str(exc.value)
        assert "dep-1" in str(exc.value)
        assert catalogue.image_exists(VLLM_REF) is True

    def test_refuses_an_image_that_is_not_here(self, catalogue):
        with pytest.raises(ValueError) as exc:
            images.delete_image("ghcr.io/example/never-pulled:1")
        assert "not present" in str(exc.value)

    def test_in_use_reads_active_deployments_and_running_containers(self, catalogue):
        """A deploy still pulling counts, a stopped one does not."""
        from spark_pulse import tools
        from spark_pulse.tools.docker import ContainerMetadata

        catalogue.run_container(
            image=VLLM_REF,
            name="spark-pulse-c1",
            env_vars={},
            metadata=ContainerMetadata(deployment="c1", image=VLLM_REF),
        )
        with (
            patch.object(
                tools.deployment_records,
                "load",
                return_value=[
                    {"id": "d1", "status": "pulling", "image_ref": VLLM_REF},
                    {"id": "d2", "status": "stopped", "image_ref": VLLM_REF},
                ],
            ),
            patch.object(
                tools.docker,
                "list_managed_containers",
                catalogue.list_managed_containers,
            ),
        ):
            in_use = images.images_in_use()

        assert in_use[VLLM_REF] == ["d1", "c1"]


# ── Distribution ─────────────────────────────────────────────────────────────


class TestSync:
    """Distribution seeds the control node's registry; nodes pull from it.

    The ``docker save | ssh docker load`` path these tests used to cover is
    gone: measured, it changed the image digest and emptied ``RepoDigests``,
    which breaks every digest-pinned deploy that follows. The first test here
    fails outright on that old code.
    """

    def test_seeding_preserves_the_advertised_digest(self, catalogue, nodes):
        advertised = images.local_digest(catalogue.image_info(VLLM_REF), VLLM_REPO)
        assert advertised

        result = images.sync_to_nodes(VLLM_REF, ["n1"], services=nodes.services)

        assert result["digest"] == advertised
        assert result["pull_ref"].endswith(f"@{advertised}")
        assert result["results"][0]["digest"] == advertised

    def test_a_copy_that_re_digests_the_image_fails_loudly(self, catalogue, nodes):
        """What save/load did. Seeding must refuse it, not report success."""
        nodes.registry.rewrite_digest = True

        with pytest.raises(RuntimeError) as raised:
            images.sync_to_nodes(VLLM_REF, ["n1"], services=nodes.services)

        assert "changed its digest" in str(raised.value)

    def test_the_reference_changes_host_while_the_digest_does_not(
        self, catalogue, nodes
    ):
        """The host is what differs per node, which is why it is its own field."""
        advertised = images.local_digest(catalogue.image_info(VLLM_REF), VLLM_REPO)

        result = images.sync_to_nodes(VLLM_REF, ["n1", "n2"], services=nodes.services)

        assert result["pull_ref"] != VLLM_REF
        assert result["pull_ref"].startswith(f"{result['registry_base']}/")
        assert not result["registry_base"].startswith("ghcr.io")
        assert result["repository"] == VLLM_REPO.partition("/")[2]
        # Two nodes, one set of three fields, one composed reference each.
        assert {r["pull_ref"] for r in result["results"]} == {result["pull_ref"]}
        assert {r["digest"] for r in result["results"]} == {advertised}

    def test_every_node_pulls_and_none_is_skipped_the_first_time(
        self, catalogue, nodes
    ):
        result = images.sync_to_nodes(VLLM_REF, ["n1", "n2"], services=nodes.services)

        assert result["ok"] is True
        assert [r["skipped"] for r in result["results"]] == [False, False]
        assert nodes.pulled("n1") == nodes.pulled("n2") == [result["pull_ref"]]

    def test_a_node_that_already_has_it_is_skipped(self, catalogue, nodes):
        images.sync_to_nodes(VLLM_REF, ["n1", "n2"], services=nodes.services)
        before = len(nodes.pulled("n1")) + len(nodes.pulled("n2"))

        again = images.sync_to_nodes(VLLM_REF, ["n1", "n2"], services=nodes.services)

        assert [r["skipped"] for r in again["results"]] == [True, True]
        assert len(nodes.pulled("n1")) + len(nodes.pulled("n2")) == before

    def test_nodes_receive_no_credentials(self, catalogue, nodes, monkeypatch):
        """The credential authenticates the control node's fetch, nothing else."""
        secrets = {"registry_username": "ci-bot", "registry_password": "s3cr3t"}
        monkeypatch.setattr(
            type(config), "get_secret", lambda self, key: secrets.get(key, "")
        )

        result = images.sync_to_nodes(VLLM_REF, ["n1"], services=nodes.services)

        assert result["nodes_need_credentials"] is False
        # What a node is asked to pull is the whole of what it is told, and it
        # is anonymous: no credential is a component of it.
        for ref in nodes.pulled("n1"):
            assert "s3cr3t" not in ref
            assert "ci-bot" not in ref
        # It is used, though — on this machine, for the copy from upstream.
        copies = [c for c in nodes.registry.commands if c[:2] == ["skopeo", "copy"]]
        assert any("ci-bot:s3cr3t" in " ".join(argv) for argv in copies)

    def test_an_unreachable_node_is_reported_and_the_others_are_not(
        self, catalogue, nodes
    ):
        nodes.fail("n2")

        result = images.sync_to_nodes(VLLM_REF, ["n1", "n2"], services=nodes.services)

        assert result["ok"] is False
        by_node = {r["node"]: r for r in result["results"]}
        assert by_node["n1"]["ok"] is True
        assert by_node["n2"]["ok"] is False
        assert "unreachable" in by_node["n2"]["error"]

    def test_save_and_load_is_gone(self):
        """No fallback: a silently wrong transfer is worse than no transfer."""
        source = Path(images.__file__).read_text()
        tree = ast.parse(source)
        prose = {
            ast.get_docstring(node, clean=False) or ""
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        }
        code = source
        for text in prose:
            code = code.replace(text, "")

        assert not hasattr(images, "_save_and_load")
        # The only mention left anywhere is the docstring saying why it went.
        assert any("docker save" in text for text in prose)
        assert "docker save" not in code
        assert "docker load" not in code

    def test_refuses_an_image_that_is_not_local(self, catalogue):
        with pytest.raises(ValueError):
            images.sync_to_nodes("ghcr.io/example/never:1", ["n1"])

    def test_refuses_an_empty_node_list(self, catalogue):
        with pytest.raises(ValueError):
            images.sync_to_nodes(VLLM_REF, [])

    def test_presence_reports_matching_ids(self, catalogue, nodes):
        """Same id is a match; a different id is present and not a match."""
        local_id = catalogue.image_info(VLLM_REF)["id"]
        nodes.docker("n1").image_info = lambda _ref: {"id": local_id}
        nodes.docker("n2").image_info = lambda _ref: {"id": "sha256:other"}

        result = images.presence(VLLM_REF, ["n1", "n2"], services=nodes.services)

        assert result["local"] is True
        by_node = {r["node"]: r for r in result["nodes"]}
        assert by_node["n1"]["matches"] is True
        assert by_node["n2"]["present"] is True
        assert by_node["n2"]["matches"] is False

    def test_presence_reports_a_node_it_could_not_ask_as_an_error(
        self, catalogue, nodes
    ):
        """Not as absent. "We could not ask" is not "it is not there"."""
        nodes.fail("n2")

        result = images.presence(VLLM_REF, ["n1", "n2"], services=nodes.services)

        by_node = {r["node"]: r for r in result["nodes"]}
        assert by_node["n2"]["present"] is False
        assert "unreachable" in by_node["n2"]["error"]
