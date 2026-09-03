"""Application configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
_SECRETS_PATH = Path.home() / ".config" / "spark-pulse" / "secrets.json"
_SETTINGS_PATH = Path.home() / ".config" / "spark-pulse" / "settings.json"

# Fields that can be overridden by environment variables.
# key = settings field name, value = env var name
_ENV_MAP: dict[str, str] = {
    "spark_vllm_path": "SPARK_VLLM_PATH",
    "webui_port": "WEBUI_PORT",
}


def _load_secrets() -> dict:
    if not _SECRETS_PATH.exists():
        return {}
    try:
        with open(_SECRETS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_secrets(data: dict) -> None:
    _SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SECRETS_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.chmod(0o600)
    tmp.rename(_SECRETS_PATH)


def _load_user_settings() -> dict:
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        with open(_SETTINGS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_user_settings(data: dict) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(_SETTINGS_PATH)


class _Config:
    """Loads settings from config.yaml with .env overrides."""

    def __init__(self):
        self._data: dict = {}
        self._env_managed: set[str] = set()
        self._load()

    def _load(self):
        # 1. Package defaults (config.yaml — overwritten on each deploy)
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH) as f:
                self._data = yaml.safe_load(f) or {}
        else:
            self._data = {}

        # 2. User overrides (persistent across deploys)
        self._data.update(_load_user_settings())

        # 3. Env var overrides (highest priority — mark as externally managed)
        self._env_managed = set()
        for field, env_var in _ENV_MAP.items():
            val = os.getenv(env_var)
            if val is not None:
                self._data[field] = int(val) if field == "webui_port" else val
                self._env_managed.add(field)

    @property
    def env_managed(self) -> list[str]:
        return sorted(self._env_managed)

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
    def cluster_enabled(self) -> bool:
        return bool(self._data.get("cluster_enabled", False))

    @property
    def job_retention_days(self) -> int:
        return int(self._data.get("job_retention_days", 7))

    @property
    def auth_enabled(self) -> bool:
        return (
            os.environ.get(
                "SPARK_PULSE_AUTH_ENABLED", str(self._data.get("auth_enabled", False))
            ).lower()
            == "true"
        )

    @property
    def benchmarking_enabled(self) -> bool:
        return (
            os.environ.get(
                "SPARK_PULSE_BENCHMARKING_ENABLED",
                str(self._data.get("benchmarking_enabled", False)),
            ).lower()
            == "true"
        )

    @property
    def oidc_provider_url(self) -> str:
        return str(self._data.get("oidc_provider_url", ""))

    @property
    def oidc_client_id(self) -> str:
        return str(self._data.get("oidc_client_id", ""))

    @property
    def oidc_client_secret(self) -> str:
        """OIDC client secret — checks settings.json first, then secrets.json."""
        if self._data.get("oidc_client_secret"):
            return str(self._data["oidc_client_secret"])
        secrets = _load_secrets()
        return str(secrets.get("oidc_client_secret", ""))

    @property
    def mcp_enabled(self) -> bool:
        return (
            os.environ.get(
                "SPARK_PULSE_MCP_ENABLED", str(self._data.get("mcp_enabled", True))
            ).lower()
            == "true"
        )

    @property
    def mcp_path(self) -> str:
        return str(self._data.get("mcp_path", "/mcp"))

    @property
    def mcp_api_token(self) -> str:
        return str(
            os.environ.get(
                "SPARK_PULSE_MCP_API_TOKEN", self._data.get("mcp_api_token", "")
            )
        )

    # ── OCI Registry Settings ──────────────────────────────────────────────

    @property
    def oci_auto_update_enabled(self) -> bool:
        return (
            os.environ.get(
                "OCI_AUTO_UPDATE_ENABLED",
                str(self._data.get("oci_auto_update_enabled", False)),
            ).lower()
            == "true"
        )

    @oci_auto_update_enabled.setter
    def oci_auto_update_enabled(self, value: bool) -> None:
        self._data["oci_auto_update_enabled"] = value

    @property
    def oci_auto_update_schedule(self) -> str:
        return str(self._data.get("oci_auto_update_schedule", "0 2 * * *"))

    @oci_auto_update_schedule.setter
    def oci_auto_update_schedule(self, value: str) -> None:
        self._data["oci_auto_update_schedule"] = value

    @property
    def oci_auto_update_overwrite_local(self) -> bool:
        return (
            os.environ.get(
                "OCI_AUTO_UPDATE_OVERWRITE_LOCAL",
                str(self._data.get("oci_auto_update_overwrite_local", False)),
            ).lower()
            == "true"
        )

    @oci_auto_update_overwrite_local.setter
    def oci_auto_update_overwrite_local(self, value: bool) -> None:
        self._data["oci_auto_update_overwrite_local"] = value

    @property
    def oci_cache_ttl_seconds(self) -> int:
        return int(
            os.environ.get(
                "OCI_CACHE_TTL_SECONDS",
                str(self._data.get("oci_cache_ttl_seconds", 300)),
            )
        )

    @property
    def oci_background_check_interval_seconds(self) -> int:
        return int(
            os.environ.get(
                "OCI_BACKGROUND_CHECK_INTERVAL_SECONDS",
                str(self._data.get("oci_background_check_interval_seconds", 900)),
            )
        )

    # ── Engines ────────────────────────────────────────────────────────────

    @property
    def default_engine(self) -> str:
        return str(
            os.environ.get(
                "SPARK_PULSE_DEFAULT_ENGINE", self._data.get("default_engine", "vllm")
            )
        )

    @property
    def engine_indexes(self) -> list[str]:
        """OCI references of engine index artifacts, in priority order."""
        raw = os.environ.get("SPARK_PULSE_ENGINE_INDEXES")
        if raw is not None:
            return [r.strip() for r in raw.split(",") if r.strip()]
        value = self._data.get("engine_indexes") or []
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return [str(v) for v in value]

    @property
    def engine_index_cache_ttl_seconds(self) -> int:
        return int(
            os.environ.get(
                "SPARK_PULSE_ENGINE_INDEX_CACHE_TTL_SECONDS",
                str(self._data.get("engine_index_cache_ttl_seconds", 3600)),
            )
        )

    @property
    def engines(self) -> dict:
        value = self._data.get("engines")
        return value if isinstance(value, dict) else {}

    def engine_enabled(self, engine: str) -> bool:
        """Whether *engine* may be selected. Unknown engines default to on."""
        entry = self.engines.get(engine)
        if isinstance(entry, dict):
            return bool(entry.get("enabled", True))
        if isinstance(entry, bool):
            return entry
        return True

    def save(self):
        # Legacy — keep for compat but user settings now go to settings.json
        user = _load_user_settings()
        _save_user_settings(user)

    def update(self, **kwargs):
        user = _load_user_settings()
        for k, v in kwargs.items():
            if v is None:  # skip None values
                continue
            if k not in self._env_managed:  # env vars take priority; don't overwrite
                user[k] = v
                self._data[k] = v
        _save_user_settings(user)

    # ── Secrets ──────────────────────────────────────────────────────────────

    @property
    def hf_token(self) -> str:
        """HuggingFace token — env var takes priority over secrets file."""
        return os.environ.get("HF_TOKEN", "") or _load_secrets().get("hf_token", "")

    def save_secret(self, key: str, value: str) -> None:
        """Persist a secret to the secrets file with restricted permissions."""
        secrets = _load_secrets()
        secrets[key] = value
        _save_secrets(secrets)

    def delete_secret(self, key: str) -> None:
        """Remove a secret from the secrets file."""
        secrets = _load_secrets()
        secrets.pop(key, None)
        _save_secrets(secrets)

    def hf_token_masked(self) -> str:
        """Return a masked representation safe to send to the UI."""
        token = self.hf_token
        if not token:
            return ""
        return "\u2022" * 8 + token[-4:]

    # ── Docker / NCCL ────────────────────────────────────────────────────────

    @property
    def docker_privileged(self) -> bool:
        return bool(self._data.get("docker", {}).get("privileged", True))

    @property
    def docker_memory_limit_gb(self) -> float | None:
        val = self._data.get("docker", {}).get("memory_limit_gb")
        return float(val) if val else None

    @property
    def docker_memory_swap_limit_gb(self) -> float | None:
        val = self._data.get("docker", {}).get("memory_swap_limit_gb")
        return float(val) if val else None

    @property
    def docker_pids_limit(self) -> int:
        return int(self._data.get("docker", {}).get("pids_limit", 4096))

    @property
    def docker_shm_size_gb(self) -> int:
        return int(self._data.get("docker", {}).get("shm_size_gb", 64))

    @property
    def docker_nofile_limit(self) -> int:
        return int(self._data.get("docker", {}).get("nofile_limit", 1048576))

    @property
    def docker_cache_dirs(self) -> list[str]:
        return self._data.get("docker", {}).get(
            "cache_dirs",
            [
                "~/.cache/vllm",
                "~/.cache/flashinfer",
                "~/.triton",
            ],
        )

    @property
    def docker_keep_entrypoint(self) -> bool:
        return bool(self._data.get("docker", {}).get("keep_entrypoint", False))

    @property
    def nccl_debug(self) -> str | None:
        return self._data.get("nccl", {}).get("debug")

    @property
    def nccl_socket_ifname(self) -> str | None:
        return self._data.get("nccl", {}).get("socket_ifname")

    @property
    def nccl_ib_hca(self) -> str | None:
        return self._data.get("nccl", {}).get("ib_hca")

    @property
    def nccl_ib_disable(self) -> bool:
        return bool(self._data.get("nccl", {}).get("ib_disable", False))

    # ── Mod Settings ───────────────────────────────────────────────────────

    @property
    def mod_network_policy(self) -> str:
        """Get mod network access policy: allow, warn, or deny."""
        return str(self._data.get("mod", {}).get("network_policy", "warn"))


config = _Config()
