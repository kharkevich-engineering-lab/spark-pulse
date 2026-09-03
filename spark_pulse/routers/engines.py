"""Engines API — registry listing, refresh and launch rendering (dry run)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.engines import (
    EngineError,
    EngineNotFound,
    NodeInfo,
    Topology,
    get_registry,
)

router = APIRouter(prefix="/api/engines", tags=["engines"])


class RenderNode(BaseModel):
    host: str
    ip: str = ""
    eth_if: str = ""
    ib_if: str = ""


class RenderRequest(BaseModel):
    recipe_id: str
    engine: str | None = None
    variant: str | None = None
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    extra_args: list[str] = Field(default_factory=list)
    nodes: list[RenderNode | str] = Field(default_factory=list)
    solo: bool = False


def _to_node(node: RenderNode | str) -> NodeInfo:
    if isinstance(node, str):
        return NodeInfo(host=node, ip=node)
    return NodeInfo(host=node.host, ip=node.ip, eth_if=node.eth_if, ib_if=node.ib_if)


@router.get("")
def list_engines():
    """All known engine specs, bundled defaults plus indexed ones."""
    registry = get_registry()
    registry.refresh_if_stale()
    return {
        "default_engine": config.default_engine,
        "engines": [
            {**spec.summary(), "enabled": registry.enabled(spec.engine)}
            for spec in registry.list()
        ],
    }


@router.post("/refresh")
def refresh_engines():
    """Re-fetch every configured engine index."""
    return get_registry().refresh(force=True)


@router.post("/render")
def render_launch(req: RenderRequest):
    """Render the per-rank launch scripts for a recipe — the dry-run primitive."""
    registry = get_registry()

    recipe = tools.recipes.get_recipe(req.recipe_id)
    if recipe is None:
        raise HTTPException(
            status_code=404, detail=f"Recipe '{req.recipe_id}' not found"
        )

    override = req.engine
    if override and req.variant:
        override = f"{override}/{req.variant}"
    elif req.variant and not override:
        override = f"{recipe.get('engine') or config.default_engine}/{req.variant}"

    try:
        engine_name, variant = registry.select(
            request_override=override,
            recipe_engine=recipe.get("engine"),
            default_engine=config.default_engine,
        )
        engine = registry.engine(engine_name, variant)
    except EngineNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    supported, reason = engine.supports(recipe)
    if not supported:
        raise HTTPException(
            status_code=400,
            detail=f"engine '{engine_name}/{variant}' cannot run this recipe: {reason}",
        )

    nodes = [] if req.solo else [_to_node(n) for n in req.nodes]
    topology = Topology(nodes=nodes)

    try:
        ranks = [
            engine.render(
                recipe,
                model=req.model,
                params=req.params,
                extra_args=req.extra_args,
                topology=topology,
                node_rank=rank,
            ).to_dict()
            for rank in range(topology.size)
        ]
    except EngineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "recipe_id": recipe.get("id", req.recipe_id),
        "engine": engine_name,
        "variant": variant,
        "image_ref": engine.default_image(),
        "model": req.model or recipe.get("model"),
        "solo": topology.is_solo,
        "nodes": [n.host for n in nodes],
        "readiness": engine.readiness_path(),
        "metrics": engine.metrics_path(),
        "ports": engine.spec.runtime.ports.model_dump(),
        "cache_mounts": engine.cache_mounts(),
        "container": engine.container_profile(),
        "ranks": ranks,
    }


@router.get("/{engine}/{variant}")
def get_engine(engine: str, variant: str = "default"):
    registry = get_registry()
    try:
        spec = registry.get(engine, variant)
    except EngineNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        **spec.summary(),
        "enabled": registry.enabled(spec.engine),
        "runtime": spec.runtime.model_dump(),
        "sources": spec.sources,
        "arch": spec.arch,
        "gpu_arch": spec.gpu_arch,
    }


@router.get("/{engine}")
def get_engine_default_variant(engine: str):
    return get_engine(engine, "default")
