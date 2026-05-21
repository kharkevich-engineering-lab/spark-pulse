import pytest
import yaml

from spark_pulse import config as config_module


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
