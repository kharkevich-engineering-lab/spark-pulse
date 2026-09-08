"""The deployment API's one entry point, over the one runtime.

There is a single way to run a deployment: :mod:`tools.native_runtime`, which
drives Docker on every node through that node's agent. This module is what the
routers and the MCP tools call, and all it does now is adapt: a plan comes back
as a dict, a status read is a live one rather than the stored row.

It used to *dispatch*, by the record's own ``runtime`` field, because a
deployment made by the removed ``run-recipe.sh`` runner could still be on disk
with a process still serving. That branch is gone with the records it was for.
"""

from __future__ import annotations

from typing import Any

from spark_pulse import tools


def list_deployments() -> list[dict[str, Any]]:
    """Every deployment, reconciled against what the nodes are running."""
    return sorted(
        tools.native_runtime.list_deployments(),
        key=lambda d: str(d.get("created_at") or ""),
    )


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
    return tools.native_runtime.stop_deployment(deployment_id)


def delete_deployment(deployment_id: str) -> bool:
    return tools.native_runtime.delete_deployment(deployment_id)


def get_logs(deployment_id: str, lines: int = 200) -> str:
    return tools.native_runtime.get_logs(deployment_id, lines)


def get_deployment(deployment_id: str) -> dict[str, Any] | None:
    """One deployment, with the status the nodes report rather than the row."""
    if tools.deployment_records.get(deployment_id) is None:
        return None
    return tools.native_runtime.status(deployment_id)
