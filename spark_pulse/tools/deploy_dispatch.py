"""The deployment API's one entry point, over a run backend.

This module is what the routers, the MCP tools, the reconciler and startup
recovery call. It asks :func:`tools.backends.backend_for` which backend owns a
run — Docker on every node through that node's agent, the only one today —
and calls the :class:`~spark_pulse.tools.backends.RunBackend` protocol on it.
All it adds is adaptation: a plan comes back as a dict, a status read is a
live one rather than the stored row. Nothing here branches on a record's
``runtime`` field — that field is a note about how a run was made, not a
choice about where to send it.
"""

from __future__ import annotations

from typing import Any

from spark_pulse import tools


def _with_serves(record: dict[str, Any]) -> dict[str, Any]:
    """The record as served, with ``serves`` always said.

    A record written before the field existed — or adopted from a container
    nobody recorded — was started as chat, since nothing else could be. Filled
    in on the way out rather than written back: the row stays what was stored.
    """
    if record.get("serves"):
        return record
    return {**record, "serves": tools.recipe_schema.serves_of(record)}


def _backend(deployment_id: str) -> Any:
    """The backend that owns a run, chosen from its record when there is one."""
    return tools.backends.backend_for(
        tools.deployment_records.get(deployment_id) or deployment_id
    )


def list_deployments() -> list[dict[str, Any]]:
    """Every deployment, reconciled against what the nodes are running."""
    return sorted(
        (_with_serves(d) for d in tools.backends.backend_for().list_runs()),
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
    """Start a deployment on the backend that owns the request.

    ``params`` is what the caller actually asked for, not the recipe's defaults
    merged in: the native path needs to tell an explicit setting apart from a
    default when it decides what to refuse and how to explain it.
    """
    return tools.backends.backend_for(params).start(
        recipe_id,
        name,
        params,
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
    return (
        tools.backends.backend_for(params)
        .plan(
            recipe_id,
            engine=engine,
            variant=variant,
            model=model,
            params=params or {},
            extra_args=extra_args or [],
            nodes=nodes,
            solo=not nodes,
            allow_missing_model=allow_missing_model,
        )
        .to_dict()
    )


def stop_deployment(deployment_id: str) -> dict[str, Any] | None:
    return _backend(deployment_id).stop(deployment_id)


def delete_deployment(deployment_id: str) -> bool:
    return _backend(deployment_id).delete(deployment_id)


def get_logs(deployment_id: str, lines: int = 200) -> str:
    return _backend(deployment_id).logs(deployment_id, lines)


def get_deployment(deployment_id: str) -> dict[str, Any] | None:
    """One deployment, with the status the nodes report rather than the row."""
    record = tools.deployment_records.get(deployment_id)
    if record is None:
        return None
    live = tools.backends.backend_for(record).status(deployment_id)
    return _with_serves(live) if live is not None else None
