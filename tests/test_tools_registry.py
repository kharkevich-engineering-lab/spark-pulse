"""Tests for the control node's image registry and digest-preserving seeding.

The module under test is the real one; only its two outward seams — the
command runner and the registry HEAD — are simulated, by the same
:class:`SimulatedRegistry` simulation mode uses. So the argv building, the
digest comparison and the failure messages asserted here are the ones that
ship.
"""

from __future__ import annotations

import importlib

import pytest

registry = importlib.import_module("spark_pulse.tools.registry")

from spark_pulse.config import config  # noqa: E402
from spark_pulse.mock.registry import SimulatedRegistry  # noqa: E402

REPO = "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm"
REF = f"{REPO}:0.1.0"
DIGEST = "sha256:" + "a1" * 32
OTHER_DIGEST = "sha256:" + "b2" * 32


@pytest.fixture
def settings(tmp_path):
    """A registry on a fixed cluster-facing address, storing under tmp."""
    return registry.RegistrySettings(
        mode=registry.MODE_LOCAL,
        address="10.0.0.1",
        port=5000,
        data_dir=str(tmp_path / "registry"),
    )


@pytest.fixture
def sim():
    return SimulatedRegistry()


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    """No upstream credential unless a test says otherwise."""
    monkeypatch.setattr(type(config), "get_secret", lambda self, key: "")


def _seed(sim, settings, ref=REF, digest=DIGEST, **kwargs):
    return registry.seed(
        ref, digest, settings=settings, runner=sim.run, http=sim.head, **kwargs
    )


# ── Modes ────────────────────────────────────────────────────────────────────


class TestModes:
    def test_the_default_is_the_full_local_registry(self):
        """A cache's blob expiry is a sharp edge; a fixed cluster wants
        determinism, so the deliberately seeded registry is the default."""
        assert registry.DEFAULT_MODE == registry.MODE_LOCAL
        assert registry.load_settings({"mode": ""}).mode == registry.MODE_LOCAL

    def test_an_unknown_mode_falls_back_to_the_default(self):
        assert registry.load_settings({"mode": "nonsense"}).mode == registry.MODE_LOCAL

    def test_proxy_mode_carries_the_upstream_and_never_expires(
        self, sim, monkeypatch, tmp_path
    ):
        secrets = {"registry_username": "ci-bot", "registry_password": "s3cr3t"}
        monkeypatch.setattr(
            type(config), "get_secret", lambda self, key: secrets.get(key, "")
        )
        proxy = registry.RegistrySettings(
            mode=registry.MODE_PROXY,
            address="10.0.0.1",
            upstream="https://ghcr.io",
            data_dir=str(tmp_path / "registry"),
        )

        registry.start(proxy, sim.run)

        run = next(c for c in sim.commands if c[:2] == ["docker", "run"])
        assert "REGISTRY_PROXY_REMOTEURL=https://ghcr.io" in run
        # 0 disables expiry outright: a blob evicted mid-cluster is exactly the
        # non-determinism this path exists to avoid.
        assert f"REGISTRY_PROXY_TTL={registry.PROXY_TTL_NEVER}" in run
        # The credential is what the *cache* uses; it is why the LAN needs none.
        assert "REGISTRY_PROXY_USERNAME=ci-bot" in run
        assert "REGISTRY_PROXY_PASSWORD=s3cr3t" in run

    def test_the_local_mode_holds_no_upstream_credential(
        self, sim, settings, monkeypatch
    ):
        monkeypatch.setattr(type(config), "get_secret", lambda self, key: "s3cr3t")

        registry.start(settings, sim.run)

        run = next(c for c in sim.commands if c[:2] == ["docker", "run"])
        assert not any("s3cr3t" in arg for arg in run)


# ── Lifecycle ────────────────────────────────────────────────────────────────


class TestLifecycle:
    def test_start_binds_to_the_cluster_facing_address_only(self, sim, settings):
        state = registry.start(settings, sim.run)

        run = next(c for c in sim.commands if c[:2] == ["docker", "run"])
        assert run[run.index("-p") + 1] == "10.0.0.1:5000:5000"
        assert state["running"] is True
        assert state["base"] == "10.0.0.1:5000"
        assert settings.image == "registry:3"

    def test_start_is_idempotent(self, sim, settings):
        registry.start(settings, sim.run)
        runs_before = [c for c in sim.commands if c[:2] == ["docker", "run"]]

        again = registry.start(settings, sim.run)

        assert again["running"] is True
        assert [c for c in sim.commands if c[:2] == ["docker", "run"]] == runs_before

    def test_a_stopped_container_is_started_in_place(self, sim, settings):
        """Restarting rather than recreating keeps the seeded blobs."""
        registry.start(settings, sim.run)
        sim._container["State"] = "exited"

        registry.start(settings, sim.run)

        assert any(c[:2] == ["docker", "start"] for c in sim.commands)
        assert len([c for c in sim.commands if c[:2] == ["docker", "run"]]) == 1

    def test_status_reports_where_it_listens_and_that_nodes_need_nothing(
        self, sim, settings
    ):
        state = registry.status(settings, sim.run)

        assert state["running"] is False
        assert state["url"] == "http://10.0.0.1:5000"
        assert state["nodes_need_credentials"] is False
        assert state["mode"] == registry.MODE_LOCAL

    def test_stop_is_idempotent(self, sim, settings):
        assert registry.stop(settings, sim.run)["running"] is False

        registry.start(settings, sim.run)
        assert registry.stop(settings, sim.run)["running"] is False
        assert registry.stop(settings, sim.run)["running"] is False

    def test_ensure_running_starts_it_once(self, sim, settings):
        registry.ensure_running(settings, sim.run)
        registry.ensure_running(settings, sim.run)

        assert len([c for c in sim.commands if c[:2] == ["docker", "run"]]) == 1


# ── The three-field reference ────────────────────────────────────────────────


class TestReference:
    def test_the_host_is_split_off_from_the_repository(self):
        assert registry.repository_path(REPO) == (
            "kharkevich-engineering-lab/spark-pulse-engine/vllm"
        )
        assert registry.registry_host(REPO) == "ghcr.io"
        assert registry.repository_path("owner/repo") == "owner/repo"
        assert registry.registry_host("owner/repo") == ""

    def test_the_reference_differs_per_host_while_the_digest_does_not(self):
        location = registry.location_for(f"{REPO}@{DIGEST}")

        upstream = location.reference()
        worker = location.reference("10.0.0.1:5000")

        assert upstream != worker
        assert upstream.startswith("ghcr.io/")
        assert worker.startswith("10.0.0.1:5000/")
        assert upstream.endswith(f"@{DIGEST}")
        assert worker.endswith(f"@{DIGEST}")
        assert location.to_dict() == {
            "registry_base": "ghcr.io",
            "repository": "kharkevich-engineering-lab/spark-pulse-engine/vllm",
            "digest": DIGEST,
        }

    def test_an_explicit_digest_wins_over_a_tag(self):
        assert registry.location_for(REF, DIGEST).digest == DIGEST

    def test_a_tagged_ref_with_no_digest_is_left_alone(self):
        assert registry.pull_reference(REF) == REF

    def test_the_seed_tag_is_stable_for_a_digest(self):
        assert registry.seed_tag(DIGEST) == "sha256-" + "a1" * 32
        assert registry.seed_tag(DIGEST, "0.1.0") == "0.1.0"


# ── Seeding ──────────────────────────────────────────────────────────────────


class TestSeeding:
    def test_skopeo_copies_the_whole_index_and_preserves_digests(self, sim, settings):
        result = _seed(sim, settings)

        copy = next(c for c in sim.commands if c[:2] == ["skopeo", "copy"])
        assert "--all" in copy
        assert "--preserve-digests" in copy
        assert "--dest-tls-verify=false" in copy
        assert copy[-2] == f"docker://{REF}"
        assert result["tool"] == "skopeo"

    def test_the_digest_survives_and_is_verified(self, sim, settings):
        result = _seed(sim, settings)

        assert result["digest"] == DIGEST
        assert result["verified"] is True
        assert result["registry_base"] == "10.0.0.1:5000"
        assert result["pull_ref"] == (
            f"10.0.0.1:5000/kharkevich-engineering-lab/spark-pulse-engine/vllm@{DIGEST}"
        )
        # And it resolves *by digest*, which is how a deploy asks for it.
        assert (
            registry.manifest_digest(result["repository"], DIGEST, settings, sim.head)
            == DIGEST
        )

    def test_a_changed_digest_fails_loudly(self, sim, settings):
        """The save/load defect, reproduced: same bytes, new identity."""
        sim.rewrite_digest = True

        with pytest.raises(registry.SeedError) as raised:
            _seed(sim, settings)

        message = str(raised.value)
        assert "changed its digest" in message
        assert DIGEST in message

    def test_a_copy_that_lands_nothing_fails(self, sim, settings):
        def _runner(argv, timeout=120):
            # A copy that exits 0 and puts nothing there is still a failure.
            if argv[:2] == ["skopeo", "copy"]:
                return registry.CommandResult(0, "", "")
            return sim.run(argv, timeout)

        with pytest.raises(registry.SeedError) as raised:
            registry.seed(REF, DIGEST, settings=settings, runner=_runner, http=sim.head)

        assert "not in the local registry" in str(raised.value)

    def test_a_failed_copy_is_reported(self, sim, settings):
        def _runner(argv, timeout=120):
            if argv[:2] == ["skopeo", "copy"]:
                return registry.CommandResult(1, "", "authentication required")
            return sim.run(argv, timeout)

        with pytest.raises(registry.SeedError) as raised:
            registry.seed(REF, DIGEST, settings=settings, runner=_runner, http=sim.head)

        assert "authentication required" in str(raised.value)

    def test_without_skopeo_it_falls_back_to_pull_tag_and_push(self, settings):
        sim = SimulatedRegistry(skopeo=False)

        result = _seed(sim, settings)

        verbs = [c[1] for c in sim.commands if c[0] == "docker"]
        assert ["pull", "tag", "push"] == [
            v for v in verbs if v in ("pull", "tag", "push")
        ]
        assert result["tool"] == "docker"
        # The weaker path is still held to the same digest.
        assert result["digest"] == DIGEST

    def test_seeding_starts_the_registry_if_it_is_not_up(self, sim, settings):
        _seed(sim, settings)

        assert any(c[:2] == ["docker", "run"] for c in sim.commands)

    def test_the_credential_is_used_for_the_fetch_and_nowhere_else(
        self, sim, settings, monkeypatch
    ):
        secrets = {"registry_username": "ci-bot", "registry_password": "s3cr3t"}
        monkeypatch.setattr(
            type(config), "get_secret", lambda self, key: secrets.get(key, "")
        )

        _seed(sim, settings)

        copy = next(c for c in sim.commands if c[:2] == ["skopeo", "copy"])
        assert copy[copy.index("--src-creds") + 1] == "ci-bot:s3cr3t"
        # Nothing in the destination half carries it: the LAN pulls anonymously.
        assert "s3cr3t" not in copy[-1]

    def test_an_empty_ref_is_refused(self, sim, settings):
        with pytest.raises(ValueError):
            _seed(sim, settings, ref="")


# ── Composing a node's reference ─────────────────────────────────────────────


class TestNodeReference:
    def test_a_seeded_image_composes_against_the_control_node(self, sim, settings):
        _seed(sim, settings)

        composed = registry.node_reference(
            REF, DIGEST, settings=settings, http=sim.head
        )

        assert composed == (
            f"10.0.0.1:5000/kharkevich-engineering-lab/spark-pulse-engine/vllm@{DIGEST}"
        )

    def test_an_image_the_registry_does_not_hold_is_left_alone(self, sim, settings):
        """Rewriting a deploy to point at a registry with nothing in it would
        fail on the node, far from the cause."""
        assert (
            registry.node_reference(REF, DIGEST, settings=settings, http=sim.head)
            == REF
        )

    def test_a_registry_holding_a_different_digest_is_left_alone(self, sim, settings):
        _seed(sim, settings)

        assert (
            registry.node_reference(REF, OTHER_DIGEST, settings=settings, http=sim.head)
            == REF
        )

    def test_an_unreachable_registry_is_not_an_error(self, settings):
        def _head(url, headers, timeout=30):
            raise registry.RegistryError("connection refused")

        assert (
            registry.node_reference(REF, DIGEST, settings=settings, http=_head) == REF
        )

    def test_describe_needs_no_registry_at_all(self, settings):
        described = registry.describe(REF, DIGEST, settings)

        assert described["registry_base"] == "10.0.0.1:5000"
        assert described["upstream"]["registry_base"] == "ghcr.io"
        assert described["digest"] == described["upstream"]["digest"] == DIGEST
        assert described["nodes_need_credentials"] is False
