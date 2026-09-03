"""Settings API."""

from fastapi import APIRouter, HTTPException

from spark_pulse.config import config

router = APIRouter(prefix="/api/settings", tags=["settings"])

_ALLOWED_SECRET_KEYS = {"hf_token"}


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
        "benchmarking_enabled": config.benchmarking_enabled,
        "env_managed": config.env_managed,
    }


@router.get("")
def get_settings():
    return _settings_response()


@router.put("")
def update_settings(req: dict):
    config.update(
        **{k: v for k, v in req.items() if v is not None and k != "env_managed"}
    )
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
