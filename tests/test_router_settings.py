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
            "env_managed",
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
