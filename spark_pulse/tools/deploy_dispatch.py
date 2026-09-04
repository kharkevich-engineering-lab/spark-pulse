"""Deployment dispatcher — routes each call to the upstream or native runtime.

Two deploy paths coexist during the native migration:

* **upstream** (``tools.deployments``) forks ``run-recipe.sh`` and tracks a PID.
* **native** (``tools.native_runtime``) drives Docker from Python.

Which one runs is decided in two different ways, on purpose:

* *Creating* follows the ``runtime`` config flag. A multi-node request under
  ``runtime: native`` is refused with an explanation rather than silently
  falling back, so nobody thinks the cluster path went native.
* *Acting on an existing deployment* (stop, delete, logs, get) follows the
  record's own ``runtime`` field. Records outlive a flag flip, so a native
  deployment stays stoppable after the flag goes back to ``upstream`` — and the
  other way round.

Both runtimes persist to the same ``deployments.json``, so listing is a merge.
"""

from __future__ import annotations

from typing import Any

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.tools.native_runtime import RUNTIME_NAME, NativeRuntimeError


def active_runtime() -> str:
    """The runtime new deployments will use."""
    return config.runtime


def uses_native(nodes: list[str] | None = None) -> bool:
    """Whether a *create* with ``nodes`` goes to the native runtime."""
    return config.runtime == RUNTIME_NAME and not nodes


def _is_native_record(record: dict[str, Any] | None) -> bool:
    return bool(record) and record.get("runtime") == RUNTIME_NAME  # type: ignore[union-attr]


def _record(deployment_id: str) -> dict[str, Any] | None:
    return next(
        (d for d in tools.deployments._load() if d.get("id") == deployment_id), None
    )


# ── Dispatch ─────────────────────────────────────────────────────────────────


def list_deployments() -> list[dict[str, Any]]:
    """Every deployment from both runtimes, native records reconciled."""
    upstream = [
        d
        for d in tools.deployments.list_deployments()
        if d.get("runtime") != RUNTIME_NAME
    ]
    native = tools.native_runtime.list_deployments()
    merged = upstream + native
    merged.sort(key=lambda d: str(d.get("created_at") or ""))
    return merged


def create_deployment(
    recipe_id: str,
    name: str,
    params: dict[str, Any],
    nodes: list[str] | None = None,
    launch_command: str = "",
    engine: str | None = None,
    variant: str | None = None,
    model: str | None = None,
    extra_args: list[str] | None = None,
    allow_missing_model: bool = False,
    raw_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a deployment on whichever runtime the config selects.

    ``params`` carries the recipe defaults merged in, which is what the
    upstream runner expects. ``raw_params`` is what the caller actually asked
    for; the native path needs it to tell an explicit setting apart from a
    recipe default.
    """
    if config.runtime != RUNTIME_NAME:
        return tools.deployments.create_deployment(
            recipe_id=recipe_id,
            name=name,
            params=params,
            nodes=nodes,
            launch_command=launch_command,
        )
    if nodes:
        raise NativeRuntimeError(
            "cluster deployments are not native yet: this build only runs solo "
            "deployments under runtime: native. Drop the nodes list or switch "
            "back to runtime: upstream."
        )
    return tools.native_runtime.create_deployment(
        recipe_id=recipe_id,
        name=name,
        params=raw_params if raw_params is not None else params,
        nodes=None,
        engine=engine,
        variant=variant,
        model=model,
        extra_args=extra_args,
        allow_missing_model=allow_missing_model,
    )


def plan_deployment(
    recipe_id: str,
    engine: str | None = None,
    variant: str | None = None,
    model: str | None = None,
    params: dict[str, Any] | None = None,
    extra_args: list[str] | None = None,
    nodes: list[str] | None = None,
    allow_missing_model: bool = True,
) -> dict[str, Any]:
    """Dry run. Always native — it is a preview, not a deploy."""
    return tools.native_runtime.plan(
        recipe_id,
        engine=engine,
        variant=variant,
        model=model,
        params=params or {},
        extra_args=extra_args or [],
        nodes=nodes,
        solo=not nodes,
        allow_missing_model=allow_missing_model,
    ).to_dict()


def stop_deployment(deployment_id: str) -> dict[str, Any] | None:
    if _is_native_record(_record(deployment_id)):
        return tools.native_runtime.stop_deployment(deployment_id)
    return tools.deployments.stop_deployment(deployment_id)


def delete_deployment(deployment_id: str) -> bool:
    if _is_native_record(_record(deployment_id)):
        return tools.native_runtime.delete_deployment(deployment_id)
    return tools.deployments.delete_deployment(deployment_id)


def get_logs(deployment_id: str, lines: int = 200) -> str:
    if _is_native_record(_record(deployment_id)):
        return tools.native_runtime.get_logs(deployment_id, lines)
    return tools.deployments.get_logs(deployment_id, lines)


def get_deployment(deployment_id: str) -> dict[str, Any] | None:
    """One deployment, live status included for native ones."""
    record = _record(deployment_id)
    if record is None:
        return None
    if _is_native_record(record):
        return tools.native_runtime.status(deployment_id)
    return record
