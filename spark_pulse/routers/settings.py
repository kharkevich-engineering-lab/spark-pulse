"""Settings API.

Three kinds of setting live here, and they are kept apart on purpose.

**Editable** — the allowlist below. Anything not on it cannot be written,
because ``config.update`` writes straight into settings.json and settings.json
is where ``auth_enabled``, ``oidc_client_secret`` and ``mcp_api_token`` are
read from. Without the allowlist, ``PUT /api/settings {"auth_enabled": false}``
turns authentication off, which would make this endpoint the way past every
other check in the system.

**Reported but not editable** — the ``environment`` block. An operator has to
be able to *see* how the process is configured to make sense of anything else:
which database it is on, whether authentication is active, which origins may
call it. Showing that is not the same as letting the browser change it, and
the ones that would be dangerous to change are exactly the ones worth showing.

**Secret** — never reported at all, only replaced. ``hf_token`` comes back
masked; the raw value leaves this process only in a deployment's environment.
"""

from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException

from spark_pulse.config import config
from spark_pulse.engines import reset_registry

router = APIRouter(prefix="/api/settings", tags=["settings"])

_ALLOWED_SECRET_KEYS = {"hf_token"}

#: The settings this endpoint may change — exactly the ones it reports back.
#:
#: ``env_managed`` and ``environment`` are absent on purpose: the first is a
#: report of which fields the environment owns, the second a report of how the
#: process is configured. Neither is a field.
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
        "docker_pull_stall_timeout_seconds",
        "benchmarking_enabled",
        "default_engine",
        "engine_indexes",
        "engine_index_cache_ttl_seconds",
        "engines",
        "docker",
        "mod",
    }
)

#: The keys the ``docker:`` block may carry.
#:
#: A second allowlist, because ``docker`` is a nested dict and an outer
#: allowlist that stops at its name would let anything through underneath it.
#: It is also what keeps dead settings dead: ``cluster_image``, ``ray_port``
#: and ``gpu_count`` sat in this block and in the UI with no reader anywhere in
#: the backend — a form that looked like configuration and configured nothing.
_ALLOWED_DOCKER_KEYS = frozenset(
    {
        "privileged",
        "memory_limit_gb",
        "memory_swap_limit_gb",
        "shm_size_gb",
        "pids_limit",
        "nofile_limit",
        "cache_dirs",
        "keep_entrypoint",
        "ipc_host",
        "network_host",
        "devices",
        "cap_add",
        "ulimits",
    }
)

#: The keys the ``mod:`` block may carry.
_ALLOWED_MOD_KEYS = frozenset({"network_policy"})

_MOD_NETWORK_POLICIES = ("allow", "warn", "deny")


def _redacted(url: str) -> str:
    """A database URL with any password removed.

    ``postgresql+psycopg://user:secret@host/db`` is configuration an operator
    needs to see and a credential they must not be shown by a page anyone
    looking over their shoulder can read. The host and database are the part
    that answers "which database am I on".
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - defensive
        return "(unparseable)"
    if not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username}:***@{host}" if parts.username else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _docker_block() -> dict:
    """The ``docker:`` block, allowlisted keys only, defaults filled in.

    Defaults come from ``config``, not from the form: a field showing 110 GB
    because the JSX says 110 tells the operator nothing about what this machine
    will actually do. Reporting only allowlisted keys also means a settings
    file carrying a stale one does not fail its own round trip.
    """
    return {
        "privileged": config.docker_privileged,
        "memory_limit_gb": config.docker_memory_limit_gb,
        "memory_swap_limit_gb": config.docker_memory_swap_limit_gb,
        "shm_size_gb": config.docker_shm_size_gb,
        "pids_limit": config.docker_pids_limit,
        "nofile_limit": config.docker_nofile_limit,
        "cache_dirs": config.docker_cache_dirs,
        "keep_entrypoint": config.docker_keep_entrypoint,
    }


def _environment_block() -> dict:
    """How this process is configured. Reported, never written from here."""
    return {
        "database_url": _redacted(config.database_url),
        "database_backend": (
            (
                config.database_url.split("://", 1)[0]
                if config.database_url
                else "sqlite"
            )
        ),
        "external_url": config.external_url,
        "cors_allowed_origins": config.cors_allowed_origins,
        "auth_enabled": config.auth_enabled,
        "oidc_provider_url": config.oidc_provider_url,
        "mcp_enabled": config.mcp_enabled,
        "mcp_path": config.mcp_path,
        "cluster_experimental": config.cluster_experimental,
        "thread_pool_size": config.thread_pool_size,
        # The control node's own image registry: how a worker node gets an
        # engine image without every node pulling from the internet. It had no
        # UI at all, so "why is this node still pulling" had no answer on any
        # page. Reported rather than editable — a change to the mode or the
        # address needs the registry container rebuilt, which is not something
        # a form field should imply it did.
        "image_registry": _image_registry(),
    }


def _image_registry() -> dict:
    """The control node's registry settings, as they resolve right now."""
    try:
        from spark_pulse.tools import registry

        resolved = registry.load_settings()
    except Exception:  # pragma: no cover - the report must not fail the page
        return {}
    return {
        "mode": resolved.mode,
        "address": resolved.address,
        "port": resolved.port,
        "upstream": resolved.upstream if resolved.mode == "proxy" else "",
    }


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
        "docker_pull_stall_timeout_seconds": config.docker_pull_stall_timeout_seconds,
        "benchmarking_enabled": config.benchmarking_enabled,
        "default_engine": config.default_engine,
        "engine_indexes": config.engine_indexes,
        "engine_index_cache_ttl_seconds": config.engine_index_cache_ttl_seconds,
        "engines": config.engines,
        "docker": _docker_block(),
        "mod": {"network_policy": config.mod_network_policy},
        "env_managed": config.env_managed,
        "environment": _environment_block(),
    }


def _checked_block(name: str, value: object, allowed: frozenset[str]) -> dict:
    """One nested block, refused rather than half-applied if it is wrong."""
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"'{name}' must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown or non-editable {name} setting(s): {', '.join(unknown)}",
        )
    return dict(value)


@router.get("")
def get_settings():
    return _settings_response()


@router.put("")
def update_settings(req: dict):
    reported_only = {"env_managed", "environment"}
    unknown = sorted(set(req) - _ALLOWED_SETTING_KEYS - reported_only)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown or non-editable setting(s): {', '.join(unknown)}",
        )

    incoming = {
        k: v for k, v in req.items() if v is not None and k not in reported_only
    }

    if "docker" in incoming:
        incoming["docker"] = _checked_block(
            "docker", incoming["docker"], _ALLOWED_DOCKER_KEYS
        )
    if "mod" in incoming:
        block = _checked_block("mod", incoming["mod"], _ALLOWED_MOD_KEYS)
        policy = block.get("network_policy")
        if policy is not None and policy not in _MOD_NETWORK_POLICIES:
            raise HTTPException(
                status_code=400,
                detail="mod.network_policy must be one of "
                f"{', '.join(_MOD_NETWORK_POLICIES)}",
            )
        incoming["mod"] = block

    config.update(**incoming)
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
