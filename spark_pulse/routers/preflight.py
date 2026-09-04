"""Pre-flight API — what would stop this deployment, before it is attempted.

The request body is the deploy plan's, field for field, on purpose: an operator
asks the pre-flight the same question they asked the preview, so the two
answers are about the same deployment and the UI can send one form to both.

This endpoint is a report and nothing else: it starts nothing and changes
nothing. The deploy path runs the same checks itself before a create and
refuses a blocked verdict — see ``routers/deployments._preflight_gate`` — so
what an operator sees here is what the gate will decide.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from spark_pulse import tools
from spark_pulse.tools.native_runtime import NativeRuntimeError

router = APIRouter(prefix="/api/preflight", tags=["preflight"])


class PreflightRequest(BaseModel):
    """The same knobs as ``POST /api/deployments/plan``. Nothing is started."""

    recipe_id: str
    name: str = ""
    engine: str | None = None
    variant: str | None = None
    model: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    extra_args: list[str] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)
    allow_missing_model: bool = True


@router.post("/run")
def run_preflight(req: PreflightRequest):
    """Check every node the deployment would touch, and report the verdict.

    A recipe that cannot be planned at all is a 400: there is no deployment to
    check. Everything a *node* is missing comes back 200 inside the report,
    because "this node has no GPU" is an answer, not a failed request.
    """
    try:
        return tools.preflight.run(
            recipe_id=req.recipe_id,
            engine=req.engine,
            variant=req.variant,
            model=req.model,
            params=req.params,
            extra_args=req.extra_args,
            nodes=req.nodes or None,
            allow_missing_model=req.allow_missing_model,
        )
    except NativeRuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
