"""Deployment dispatcher — one runtime, plus a tombstone for the old one.

There is a single way to start a deployment: :mod:`tools.native_runtime`,
which drives Docker from Python. The upstream runtime — fork ``run-recipe.sh``
out of a spark-vllm-docker checkout, track a PID, SIGTERM the process group —
is gone, and nothing can create such a deployment any more.

What can still exist is its *records*. An operator who upgrades while an
upstream deployment is serving keeps a row in ``deployments.json`` and a
process on the machine, and deleting the code without deleting that would
leave a GPU held by something the control plane no longer admits exists. So
acting on an existing deployment still routes by the record's own ``runtime``
field: native records go to the native runtime, everything else goes to
:mod:`tools.deployment_records`, which can list, log, stop and delete a legacy
deployment and nothing else.

Both live in the same ``deployments.json``, so listing is a merge.
"""

from __future__ import annotations

from typing import Any

from spark_pulse import tools
from spark_pulse.tools.native_runtime import RUNTIME_NAME


def _is_native_record(record: dict[str, Any] | None) -> bool:
    return bool(record) and record.get("runtime") == RUNTIME_NAME  # type: ignore[union-attr]


def _record(deployment_id: str) -> dict[str, Any] | None:
    return tools.deployment_records.get(deployment_id)


# ── Dispatch ─────────────────────────────────────────────────────────────────


def list_deployments() -> list[dict[str, Any]]:
    """Every deployment: native ones reconciled, legacy ones checked by PID."""
    legacy = tools.deployment_records.list_legacy()
    native = tools.native_runtime.list_deployments()
    merged = legacy + native
    merged.sort(key=lambda d: str(d.get("created_at") or ""))
    return merged


def create_deployment(
    recipe_id: str,
    name: str,
    params: dict[str, Any],
    nodes: list[str] | None = None,
    engine: str | None = None,
    variant: str | None = None,
    model: str | None = None,
    extra_args: list[str] | None = None,
    allow_missing_model: bool = False,
) -> dict[str, Any]:
    """Start a deployment. Always native — there is nothing else to start.

    ``params`` is what the caller actually asked for, not the recipe's defaults
    merged in: the native path needs to tell an explicit setting apart from a
    default when it decides what to refuse and how to explain it.
    """
    return tools.native_runtime.create_deployment(
        recipe_id=recipe_id,
        name=name,
        params=params,
        nodes=nodes,
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
    """Dry run: resolve everything a create would, start nothing."""
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
    return tools.deployment_records.stop_legacy(deployment_id)


def delete_deployment(deployment_id: str) -> bool:
    if _is_native_record(_record(deployment_id)):
        return tools.native_runtime.delete_deployment(deployment_id)
    # A legacy record is stopped before it is dropped, for the same reason a
    # native one is: forgetting a deployment is not the same as ending it, and
    # only one of those frees the GPU.
    tools.deployment_records.stop_legacy(deployment_id)
    return tools.deployment_records.delete(deployment_id)


def get_logs(deployment_id: str, lines: int = 200) -> str:
    if _is_native_record(_record(deployment_id)):
        return tools.native_runtime.get_logs(deployment_id, lines)
    return tools.deployment_records.logs_legacy(deployment_id, lines)


def get_deployment(deployment_id: str) -> dict[str, Any] | None:
    """One deployment, live status included for native ones."""
    record = _record(deployment_id)
    if record is None:
        return None
    if _is_native_record(record):
        return tools.native_runtime.status(deployment_id)
    return record
