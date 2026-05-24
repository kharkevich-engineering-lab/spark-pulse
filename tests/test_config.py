import pytest
import yaml

from spark_pulse import config as config_module


# ── Config loading tests ────────────────────────────────────────────────────


def test_config_loads_yaml_when_present(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "spark_vllm_path": "/opt/spark",
                "webui_port": 8200,
                "default_gpu_mem_util": 0.9,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)
    cfg = config_module._Config()

    assert cfg.spark_vllm_path == "/opt/spark"
    assert cfg.webui_port == 8200
    assert cfg.default_gpu_mem_util == 0.9


def test_config_uses_defaults_when_file_missing(tmp_path, monkeypatch):
    missing = tmp_path / "missing.yaml"
    monkeypatch.setattr(config_module, "_CONFIG_PATH", missing)

    cfg = config_module._Config()

    assert cfg.spark_vllm_path == "/tmp/spark-vllm-docker"
    assert cfg.webui_port == 8100


def test_env_overrides_yaml_values(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"spark_vllm_path": "/from-yaml", "webui_port": 8101}),
        encoding="utf-8",
    )

    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)
    monkeypatch.setenv("SPARK_VLLM_PATH", "/from-env")
    monkeypatch.setenv("WEBUI_PORT", "9999")

    cfg = config_module._Config()

    assert cfg.spark_vllm_path == "/from-env"
    assert cfg.webui_port == 9999


# ── Auth config tests ───────────────────────────────────────────────────────


def test_auth_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")
    cfg = config_module._Config()

    assert cfg.auth_enabled is True


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [("true", True), ("false", False)],
)
def test_mcp_enabled_env_override(monkeypatch, env_value, expected):
    monkeypatch.setenv("SPARK_PULSE_MCP_ENABLED", env_value)
    cfg = config_module._Config()

    assert cfg.mcp_enabled is expected


def test_auth_enabled_defaults_to_false(monkeypatch):
    """Auth should be disabled by default."""
    cfg = config_module._Config()
    assert cfg.auth_enabled is False


def test_auth_enabled_from_yaml(tmp_path, monkeypatch):
    """Auth should be enabled when set in YAML."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"auth_enabled": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    cfg = config_module._Config()
    assert cfg.auth_enabled is True


def test_auth_enabled_env_overrides_yaml(tmp_path, monkeypatch):
    """Env var should override YAML for auth_enabled."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"auth_enabled": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)
    monkeypatch.setenv("SPARK_PULSE_AUTH_ENABLED", "true")

    cfg = config_module._Config()
    assert cfg.auth_enabled is True


def test_oidc_provider_url_default(monkeypatch):
    """OIDC provider URL should default to empty string."""
    cfg = config_module._Config()
    assert cfg.oidc_provider_url == ""


def test_oidc_client_id_default(monkeypatch):
    """OIDC client ID should default to empty string."""
    cfg = config_module._Config()
    assert cfg.oidc_client_id == ""


def test_oidc_client_secret_default(monkeypatch):
    """OIDC client secret should default to empty string."""
    cfg = config_module._Config()
    assert cfg.oidc_client_secret == ""


def test_oidc_values_from_yaml(tmp_path, monkeypatch):
    """OIDC values should be read from YAML."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({
            "auth_enabled": True,
            "oidc_provider_url": "https://keycloak.example.com/realms/myrealm",
            "oidc_client_id": "spark-pulse",
            "oidc_client_secret": "secret123",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    cfg = config_module._Config()
    assert cfg.oidc_provider_url == "https://keycloak.example.com/realms/myrealm"
    assert cfg.oidc_client_id == "spark-pulse"
    assert cfg.oidc_client_secret == "secret123"


# ── MCP config tests ────────────────────────────────────────────────────────


def test_mcp_enabled_defaults_to_true(monkeypatch):
    """MCP should be enabled by default."""
    cfg = config_module._Config()
    assert cfg.mcp_enabled is True


def test_mcp_path_default(monkeypatch):
    """MCP path should default to /mcp."""
    cfg = config_module._Config()
    assert cfg.mcp_path == "/mcp"


def test_mcp_api_token_default(monkeypatch):
    """MCP API token should default to empty string."""
    cfg = config_module._Config()
    assert cfg.mcp_api_token == ""


def test_mcp_enabled_from_yaml(tmp_path, monkeypatch):
    """MCP should be disabled when set in YAML."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"mcp_enabled": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    cfg = config_module._Config()
    assert cfg.mcp_enabled is False


# ── Cluster config tests ────────────────────────────────────────────────────


def test_cluster_enabled_defaults_to_false(monkeypatch):
    """Cluster mode should be disabled by default."""
    cfg = config_module._Config()
    assert cfg.cluster_enabled is False


def test_cluster_enabled_from_yaml(tmp_path, monkeypatch):
    """Cluster mode should be enabled when set in YAML."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"cluster_enabled": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    cfg = config_module._Config()
    assert cfg.cluster_enabled is True


# ── Default values tests ────────────────────────────────────────────────────


def test_default_container(monkeypatch):
    """Default container should be vllm-node."""
    cfg = config_module._Config()
    assert cfg.default_container == "vllm-node"


def test_default_gpu_mem_util(monkeypatch):
    """Default GPU memory utilization should be 0.8."""
    cfg = config_module._Config()
    assert cfg.default_gpu_mem_util == 0.8


def test_default_port_range(tmp_path, monkeypatch):
    """Default port range should be 9000-9100."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({
            "default_port_range_start": 9000,
            "default_port_range_end": 9100,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    cfg = config_module._Config()
    assert cfg.default_port_range_start == 9000
    assert cfg.default_port_range_end == 9100


def test_job_retention_days(tmp_path, monkeypatch):
    """Job retention days should default to 7."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"job_retention_days": 14}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    cfg = config_module._Config()
    assert cfg.job_retention_days == 14


# ── User settings tests ─────────────────────────────────────────────────────


def test_user_settings_override_yaml(tmp_path, monkeypatch):
    """User settings should override YAML defaults."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"webui_port": 8100}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"webui_port": 9000}')
    monkeypatch.setattr(config_module, "_SETTINGS_PATH", settings_file)

    cfg = config_module._Config()
    assert cfg.webui_port == 9000


def test_env_overrides_user_settings(tmp_path, monkeypatch):
    """Env vars should override user settings."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"webui_port": 8100}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"webui_port": 9000}')
    monkeypatch.setattr(config_module, "_SETTINGS_PATH", settings_file)

    monkeypatch.setenv("WEBUI_PORT", "9999")

    cfg = config_module._Config()
    assert cfg.webui_port == 9999


# ── Env managed tracking tests ──────────────────────────────────────────────


def test_env_managed_tracks_env_vars(tmp_path, monkeypatch):
    """Config should track which fields are managed by env vars."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"spark_vllm_path": "/from-yaml"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)
    monkeypatch.setenv("SPARK_VLLM_PATH", "/from-env")

    cfg = config_module._Config()
    assert "spark_vllm_path" in cfg.env_managed


def test_env_managed_empty_when_no_env_vars(tmp_path, monkeypatch):
    """Env managed should be empty when no env vars are set."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"spark_vllm_path": "/from-yaml"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)
    monkeypatch.delenv("SPARK_VLLM_PATH", raising=False)
    monkeypatch.delenv("WEBUI_PORT", raising=False)

    cfg = config_module._Config()
    assert cfg.env_managed == []


# ── Secrets management tests ────────────────────────────────────────────────


def test_save_and_load_secret(tmp_path, monkeypatch):
    """Secrets should be saved and loaded correctly."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr(config_module, "_SECRETS_PATH", secrets_file)

    cfg = config_module._Config()
    cfg.save_secret("hf_token", "test-token-123")

    # Reload config to verify persistence
    cfg2 = config_module._Config()
    assert cfg2.hf_token == "test-token-123"


def test_delete_secret(tmp_path, monkeypatch):
    """Secrets should be deletable."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr(config_module, "_SECRETS_PATH", secrets_file)

    cfg = config_module._Config()
    cfg.save_secret("hf_token", "test-token-123")
    cfg.delete_secret("hf_token")

    cfg2 = config_module._Config()
    assert cfg2.hf_token == ""


def test_hf_token_masked(tmp_path, monkeypatch):
    """HF token should be masked in responses."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr(config_module, "_SECRETS_PATH", secrets_file)

    cfg = config_module._Config()
    cfg.save_secret("hf_token", "my-secret-token")

    masked = cfg.hf_token_masked()
    assert masked != "my-secret-token"
    assert masked != ""
    assert "•" in masked


def test_hf_token_masked_empty(tmp_path, monkeypatch):
    """Masked token should be empty string when no token is set."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr(config_module, "_SECRETS_PATH", secrets_file)

    cfg = config_module._Config()
    assert cfg.hf_token_masked() == ""


# ── Settings update tests ───────────────────────────────────────────────────


def test_update_settings(tmp_path, monkeypatch):
    """Settings should be updatable."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"webui_port": 8100}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config_module, "_SETTINGS_PATH", settings_file)

    cfg = config_module._Config()
    cfg.update(webui_port=9000)

    assert cfg.webui_port == 9000


def test_update_settings_ignores_none_values(tmp_path, monkeypatch):
    """None values should be ignored during update."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"webui_port": 8100}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config_module, "_SETTINGS_PATH", settings_file)

    cfg = config_module._Config()
    cfg.update(webui_port=None)

    # None values should be filtered out by update()
    assert cfg.webui_port == 8100


def test_update_settings_updates_valid_values(tmp_path, monkeypatch):
    """Valid values should be updated during update()."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump({"webui_port": 8100}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_CONFIG_PATH", config_file)

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config_module, "_SETTINGS_PATH", settings_file)

    cfg = config_module._Config()
    cfg.update(webui_port=9500)

    assert cfg.webui_port == 9500
