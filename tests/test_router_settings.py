"""Tests for the /api/settings router.

Only the read half was covered. The write half — which persists to the
operator's own ``~/.config/spark-pulse`` — had no tests at all, which is
precisely why it needs them written carefully: every test here redirects both
config files into tmp_path first, and the process-wide config singleton is
snapshotted and restored, so a run leaves the developer's machine untouched.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from spark_pulse import config as config_module
from spark_pulse.app import create_app
from spark_pulse.config import config


@pytest.fixture(autouse=True)
def private_config_files(tmp_path, monkeypatch):
    """Settings and secrets written into tmp_path, never into ``$HOME``."""
    settings = tmp_path / "settings.json"
    secrets = tmp_path / "secrets.json"
    monkeypatch.setattr(config_module, "_SETTINGS_PATH", settings)
    monkeypatch.setattr(config_module, "_SECRETS_PATH", secrets)
    monkeypatch.delenv("HF_TOKEN", raising=False)

    snapshot = dict(config._data)
    yield {"settings": settings, "secrets": secrets}
    config._data.clear()
    config._data.update(snapshot)


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


# ── Reading ──────────────────────────────────────────────────────────────────


class TestGetSettings:
    def test_the_form_is_given_every_field_it_renders(self, client):
        body = client.get("/api/settings").json()

        assert set(body) == {
            "spark_vllm_path",
            "default_container",
            "default_gpu_mem_util",
            "default_port_range_start",
            "default_port_range_end",
            "webui_port",
            "cluster_enabled",
            "job_retention_days",
            "runtime",
            "deploy_ready_timeout_seconds",
            "benchmarking_enabled",
            "default_engine",
            "engine_indexes",
            "engine_index_cache_ttl_seconds",
            "engines",
            "docker_pull_stall_timeout_seconds",
            "docker",
            "mod",
            "env_managed",
            "environment",
        }

    def test_a_field_the_environment_owns_is_named_as_such(self, client, monkeypatch):
        monkeypatch.setenv("WEBUI_PORT", "9999")
        config._load()

        body = client.get("/api/settings").json()

        assert body["webui_port"] == 9999
        assert "webui_port" in body["env_managed"]


# ── Writing ──────────────────────────────────────────────────────────────────


class TestUpdateSettings:
    def test_a_saved_setting_is_persisted_and_read_back(
        self, client, private_config_files
    ):
        body = client.put("/api/settings", json={"default_container": "my-node"}).json()

        assert body["default_container"] == "my-node"
        assert json.loads(private_config_files["settings"].read_text()) == {
            "default_container": "my-node"
        }
        assert client.get("/api/settings").json()["default_container"] == "my-node"

    def test_a_null_leaves_the_current_value_alone(self, client):
        client.put("/api/settings", json={"default_container": "my-node"})

        body = client.put("/api/settings", json={"default_container": None}).json()

        assert body["default_container"] == "my-node"

    def test_env_managed_is_never_written_back(self, client, private_config_files):
        client.put("/api/settings", json={"env_managed": ["webui_port"]})

        assert "env_managed" not in json.loads(
            private_config_files["settings"].read_text()
        )

    def test_a_field_the_environment_owns_cannot_be_overwritten(
        self, client, monkeypatch, private_config_files
    ):
        monkeypatch.setenv("WEBUI_PORT", "9999")
        config._load()

        body = client.put("/api/settings", json={"webui_port": 1234}).json()

        assert body["webui_port"] == 9999
        assert json.loads(private_config_files["settings"].read_text()) == {}

    def test_changing_an_engine_setting_rebuilds_the_engine_registry(
        self, client, monkeypatch
    ):
        from spark_pulse.routers import settings as settings_router

        reset_calls: list[int] = []
        monkeypatch.setattr(
            settings_router, "reset_registry", lambda: reset_calls.append(1)
        )

        client.put("/api/settings", json={"default_engine": "sglang"})
        assert len(reset_calls) == 1

        client.put("/api/settings", json={"engine_index_cache_ttl_seconds": 60})
        assert len(reset_calls) == 2

    def test_changing_an_unrelated_setting_leaves_the_registry_alone(
        self, client, monkeypatch
    ):
        from spark_pulse.routers import settings as settings_router

        monkeypatch.setattr(
            settings_router,
            "reset_registry",
            lambda: pytest.fail("the engine registry must not be rebuilt"),
        )

        response = client.put("/api/settings", json={"job_retention_days": 5})

        assert response.status_code == 200
        assert response.json()["job_retention_days"] == 5


# ── Secrets ──────────────────────────────────────────────────────────────────


class TestSecrets:
    def test_an_unset_token_reads_back_empty(self, client):
        assert client.get("/api/settings/secrets").json() == {"hf_token": ""}

    def test_a_saved_token_is_only_ever_returned_masked(
        self, client, private_config_files
    ):
        body = client.put(
            "/api/settings/secrets", json={"hf_token": "hf_verysecret1234"}
        ).json()

        assert body == {"hf_token": "•" * 8 + "1234"}
        assert client.get("/api/settings/secrets").json() == body
        # The real token is on disk, and nowhere in the response.
        assert json.loads(private_config_files["secrets"].read_text()) == {
            "hf_token": "hf_verysecret1234"
        }

    def test_the_secrets_file_is_readable_only_by_its_owner(
        self, client, private_config_files
    ):
        client.put("/api/settings/secrets", json={"hf_token": "hf_secret"})

        mode = private_config_files["secrets"].stat().st_mode & 0o777

        assert mode == 0o600

    def test_surrounding_whitespace_is_not_part_of_the_token(
        self, client, private_config_files
    ):
        client.put("/api/settings/secrets", json={"hf_token": "  hf_padded  "})

        assert json.loads(private_config_files["secrets"].read_text()) == {
            "hf_token": "hf_padded"
        }

    def test_saving_an_empty_token_clears_it(self, client, private_config_files):
        client.put("/api/settings/secrets", json={"hf_token": "hf_secret"})

        body = client.put("/api/settings/secrets", json={"hf_token": "   "}).json()

        assert body == {"hf_token": ""}
        assert json.loads(private_config_files["secrets"].read_text()) == {}

    def test_deleting_the_token_clears_it(self, client, private_config_files):
        client.put("/api/settings/secrets", json={"hf_token": "hf_secret"})

        response = client.delete("/api/settings/secrets/hf_token")

        assert response.json() == {"deleted": "hf_token"}
        assert json.loads(private_config_files["secrets"].read_text()) == {}

    def test_deleting_a_token_that_was_never_saved_is_not_an_error(self, client):
        assert client.delete("/api/settings/secrets/hf_token").json() == {
            "deleted": "hf_token"
        }

    @pytest.mark.parametrize("key", ["aws_secret", "hf_token_2", "password"])
    def test_only_known_secrets_can_be_saved(self, client, key, private_config_files):
        response = client.put("/api/settings/secrets", json={key: "value"})

        assert response.status_code == 400
        assert response.json()["detail"] == f"Unknown secret key: {key}"
        assert not private_config_files["secrets"].exists()

    def test_only_known_secrets_can_be_deleted(self, client):
        response = client.delete("/api/settings/secrets/aws_secret")

        assert response.status_code == 400
        assert response.json()["detail"] == "Unknown secret key: aws_secret"


# ── The nested blocks, and what must stay unwritable ─────────────────────────


class TestTheDockerBlock:
    """``docker:`` is what every deployment's container is actually built with.

    It was reported by no endpoint and refused by the allowlist, so the page
    that showed it rendered its defaults from literals in the JSX — values the
    machine did not have — and posting them back was a 400. The block round
    trips now, and the keys it may carry are named.
    """

    def test_it_is_reported_with_the_values_in_force(self, client):
        block = client.get("/api/settings").json()["docker"]

        assert block["shm_size_gb"] == config.docker_shm_size_gb
        assert block["privileged"] == config.docker_privileged
        assert block["cache_dirs"] == config.docker_cache_dirs

    def test_a_saved_value_is_persisted_and_read_back(
        self, client, private_config_files
    ):
        body = client.put("/api/settings", json={"docker": {"shm_size_gb": 8}}).json()

        assert body["docker"]["shm_size_gb"] == 8
        assert json.loads(private_config_files["settings"].read_text()) == {
            "docker": {"shm_size_gb": 8}
        }

    def test_no_limit_is_not_a_limit_of_zero(self, client):
        """``null`` means "the engine decides"; 0 would cap it at nothing."""
        body = client.put(
            "/api/settings", json={"docker": {"memory_limit_gb": None}}
        ).json()

        assert body["docker"]["memory_limit_gb"] is None

    @pytest.mark.parametrize("dead", ["cluster_image", "ray_port", "gpu_count"])
    def test_a_setting_nothing_reads_is_refused(
        self, client, dead, private_config_files
    ):
        """These three sat in this block and in the settings form with no
        reader anywhere in the backend. Refusing them is what keeps them from
        coming back as configuration that configures nothing."""
        response = client.put("/api/settings", json={"docker": {dead: 1}})

        assert response.status_code == 400
        assert dead in response.json()["detail"]
        assert not private_config_files["settings"].exists()

    def test_a_docker_block_that_is_not_an_object_is_refused(self, client):
        response = client.put("/api/settings", json={"docker": "privileged"})

        assert response.status_code == 400
        assert "must be an object" in response.json()["detail"]

    def test_a_stale_key_in_the_file_does_not_break_the_round_trip(
        self, client, private_config_files
    ):
        """An operator upgrading has ``ray_port`` in their settings.json. The
        GET must not hand back a body its own PUT would then refuse."""
        private_config_files["settings"].write_text(
            json.dumps({"docker": {"ray_port": 29501, "shm_size_gb": 16}})
        )
        config._load()

        body = client.get("/api/settings").json()

        assert "ray_port" not in body["docker"]
        assert client.put("/api/settings", json=body).status_code == 200


class TestTheModBlock:
    def test_the_policy_round_trips(self, client):
        body = client.put(
            "/api/settings", json={"mod": {"network_policy": "deny"}}
        ).json()

        assert body["mod"]["network_policy"] == "deny"

    def test_a_policy_nothing_implements_is_refused(self, client):
        """The three the checker knows are the three it can enforce; anything
        else would read as configured and behave as ``warn``."""
        response = client.put("/api/settings", json={"mod": {"network_policy": "off"}})

        assert response.status_code == 400
        assert "allow, warn, deny" in response.json()["detail"]

    def test_an_unknown_mod_key_is_refused(self, client):
        response = client.put("/api/settings", json={"mod": {"sandbox": True}})

        assert response.status_code == 400
        assert "sandbox" in response.json()["detail"]


class TestTheEnvironmentReport:
    """Configuration the operator must see and the browser must not change."""

    def test_it_is_reported(self, client):
        environment = client.get("/api/settings").json()["environment"]

        assert environment["auth_enabled"] == config.auth_enabled
        assert environment["cors_allowed_origins"] == config.cors_allowed_origins
        assert environment["thread_pool_size"] == config.thread_pool_size

    def test_sending_it_back_writes_nothing(self, client, private_config_files):
        body = client.get("/api/settings").json()
        body["environment"]["auth_enabled"] = not body["environment"]["auth_enabled"]

        assert client.put("/api/settings", json=body).status_code == 200

        written = json.loads(private_config_files["settings"].read_text())
        assert "environment" not in written
        assert "auth_enabled" not in written

    @pytest.mark.parametrize(
        "key", ["auth_enabled", "oidc_client_secret", "mcp_api_token", "external_url"]
    )
    def test_the_dangerous_settings_stay_unwritable(
        self, client, key, private_config_files
    ):
        """``config.update`` writes into settings.json, which is where these
        are read from. A PUT that could set ``auth_enabled`` would make this
        endpoint the way past every other check in the system."""
        response = client.put("/api/settings", json={key: "anything"})

        assert response.status_code == 400
        assert key in response.json()["detail"]
        assert not private_config_files["settings"].exists()

    def test_a_database_password_is_not_put_on_the_page(self, client, monkeypatch):
        """The host and database answer "which database am I on". The password
        is a credential, and this page is readable over a shoulder."""
        monkeypatch.setenv(
            "SPARK_PULSE_DATABASE_URL", "postgresql+psycopg://pulse:hunter2@db/pulse"
        )

        reported = client.get("/api/settings").json()["environment"]["database_url"]

        assert "hunter2" not in reported
        assert "db/pulse" in reported

    def test_a_url_with_no_password_is_left_alone(self, client, monkeypatch):
        monkeypatch.setenv(
            "SPARK_PULSE_DATABASE_URL", "sqlite:////var/lib/spark-pulse/db.sqlite"
        )

        reported = client.get("/api/settings").json()["environment"]["database_url"]

        assert reported == "sqlite:////var/lib/spark-pulse/db.sqlite"

    def test_the_environment_wins_over_settings_json(self, client, monkeypatch):
        """The bug the e2e backend check caught.

        ``config.database_url`` reads settings.json alone; the engine resolves
        ``SPARK_PULSE_DATABASE_URL`` first. Reporting config's view meant a
        process told to use PostgreSQL by its environment — how a systemd unit
        or a container sets it — showed "sqlite" on the page an operator opens
        to find out which database they are on, while every table sat in
        PostgreSQL.
        """
        config._data["database_url"] = "sqlite:////wrong/answer.db"
        monkeypatch.setenv(
            "SPARK_PULSE_DATABASE_URL", "postgresql+psycopg://pulse@db/spark_pulse"
        )

        environment = client.get("/api/settings").json()["environment"]

        assert environment["database_backend"] == "postgresql+psycopg"
        assert "wrong/answer.db" not in environment["database_url"]

    def test_the_image_registry_is_reported(self, client):
        """How a worker node gets an engine image without every node pulling
        from the internet. It had no UI at all, so "why is this node still
        pulling" had no answer on any page."""
        reported = client.get("/api/settings").json()["environment"]["image_registry"]

        assert reported["mode"] in ("local", "proxy")
        assert reported["port"]

    def test_the_upstream_is_only_reported_for_a_proxy(self, client):
        """A full registry has no upstream to name; reporting one would say
        this node caches something it actually serves itself."""
        config._data["image_registry"] = {"mode": "local"}

        assert (
            client.get("/api/settings").json()["environment"]["image_registry"][
                "upstream"
            ]
            == ""
        )
