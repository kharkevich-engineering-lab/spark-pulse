"""Settings API."""

from fastapi import APIRouter, HTTPException

from spark_pulse.config import config
from spark_pulse.engines import reset_registry

router = APIRouter(prefix="/api/settings", tags=["settings"])

_ALLOWED_SECRET_KEYS = {"hf_token"}

#: The settings this endpoint may change — exactly the ones it reports back.
#:
#: An allowlist rather than "anything the client sends", because ``update``
#: writes straight into settings.json and settings.json is where
#: ``auth_enabled``, ``oidc_client_secret`` and ``mcp_api_token`` are read
#: from. Without this, ``PUT /api/settings {"auth_enabled": false}`` turns
#: authentication off, which makes the settings endpoint the way past every
#: other check in the system.
#:
#: ``env_managed`` is absent on purpose: it is a report of which fields the
#: environment owns, not a field.
_ALLOWED_SETTING_KEYS = frozenset(
    {
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
    }
)


def _settings_response() -> dict:
    return {
        "spark_vllm_path": config.spark_vllm_path,
        "default_container": config.default_container,
        "default_gpu_mem_util": config.default_gpu_mem_util,
        "default_port_range_start": config.default_port_range_start,
        "default_port_range_end": config.default_port_range_end,
        "webui_port": config.webui_port,
        "cluster_enabled": config.cluster_enabled,
        "job_retention_days": config.job_retention_days,
        "runtime": config.runtime,
        "deploy_ready_timeout_seconds": config.deploy_ready_timeout_seconds,
        "benchmarking_enabled": config.benchmarking_enabled,
        "default_engine": config.default_engine,
        "engine_indexes": config.engine_indexes,
        "engine_index_cache_ttl_seconds": config.engine_index_cache_ttl_seconds,
        "engines": config.engines,
        "env_managed": config.env_managed,
    }


@router.get("")
def get_settings():
    return _settings_response()


@router.put("")
def update_settings(req: dict):
    unknown = sorted(set(req) - _ALLOWED_SETTING_KEYS - {"env_managed"})
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown or non-editable setting(s): {', '.join(unknown)}",
        )
    config.update(
        **{k: v for k, v in req.items() if v is not None and k != "env_managed"}
    )
    # Engine settings feed the registry; drop it so the next call rebuilds.
    if any(k.startswith(("engine", "default_engine")) for k in req):
        reset_registry()
    return _settings_response()


@router.get("/secrets")
def get_secrets():
    """Return masked secret values — never exposes the raw token."""
    return {"hf_token": config.hf_token_masked()}


@router.put("/secrets")
def save_secrets(req: dict):
    """Persist one or more secrets to the local secrets file (chmod 600)."""
    for key, value in req.items():
        if key not in _ALLOWED_SECRET_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown secret key: {key}")
        token = str(value).strip()
        if not token:
            config.delete_secret(key)
        else:
            config.save_secret(key, token)
    return {"hf_token": config.hf_token_masked()}


@router.delete("/secrets/{key}")
def delete_secret(key: str):
    if key not in _ALLOWED_SECRET_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown secret key: {key}")
    config.delete_secret(key)
    return {"deleted": key}
