"""Application configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


class _Config:
    """Loads settings from config.yaml with .env overrides."""

    def __init__(self):
        self._data: dict = {}
        self._load()

    def _load(self):
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as f:
                self._data = yaml.safe_load(f) or {}
        else:
            self._data = {}
        # .env overrides
        self._data["spark_vllm_path"] = os.getenv(
            "SPARK_VLLM_PATH", self._data.get("spark_vllm_path", "/tmp/spark-vllm-docker")
        )
        self._data["webui_port"] = int(
            os.getenv("WEBUI_PORT", str(self._data.get("webui_port", 8100)))
        )

    @property
    def spark_vllm_path(self) -> str:
        return str(self._data.get("spark_vllm_path", "/tmp/spark-vllm-docker"))

    @property
    def default_container(self) -> str:
        return str(self._data.get("default_container", "vllm-node"))

    @property
    def default_gpu_mem_util(self) -> float:
        return float(self._data.get("default_gpu_mem_util", 0.8))

    @property
    def default_port_range_start(self) -> int:
        return int(self._data.get("default_port_range_start", 9000))

    @property
    def default_port_range_end(self) -> int:
        return int(self._data.get("default_port_range_end", 9100))

    @property
    def webui_port(self) -> int:
        return int(self._data.get("webui_port", 8100))

    @property
    def auth_enabled(self) -> bool:
        return os.environ.get("SPARK_PULSE_AUTH_ENABLED", str(self._data.get("auth_enabled", False))).lower() == "true"

    @property
    def oidc_provider_url(self) -> str:
        return str(self._data.get("oidc_provider_url", ""))

    @property
    def oidc_client_id(self) -> str:
        return str(self._data.get("oidc_client_id", ""))

    @property
    def oidc_client_secret(self) -> str:
        return str(self._data.get("oidc_client_secret", ""))

    @property
    def mcp_enabled(self) -> bool:
        return os.environ.get("SPARK_PULSE_MCP_ENABLED", str(self._data.get("mcp_enabled", True))).lower() == "true"

    @property
    def mcp_path(self) -> str:
        return str(self._data.get("mcp_path", "/mcp"))

    @property
    def mcp_api_token(self) -> str:
        return str(os.environ.get("SPARK_PULSE_MCP_API_TOKEN", self._data.get("mcp_api_token", "")))

    def save(self):
        with open(_CONFIG_PATH, "w") as f:
            yaml.dump(self._data, f, default_flow_style=False)

    def update(self, **kwargs):
        for k, v in kwargs.items():
            self._data[k] = v
        self.save()


config = _Config()
