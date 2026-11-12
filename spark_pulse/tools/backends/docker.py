"""Docker on every node, through each node's agent: today's runtime.

A thin delegate to :mod:`tools.native_runtime`, which still owns both *what*
to run and *how* to run it on Docker; the split between those comes later in
#175. ``native_runtime`` is looked up through ``spark_pulse.tools`` on every
call rather than imported, so the simulation switch and every test that
patches ``tools.native_runtime.<fn>`` keep applying.

Podman is not a separate backend: it is a fact about a node that the agent
acts on (#165).
"""

from __future__ import annotations

from typing import Any

from spark_pulse import tools


class DockerBackend:
    """:class:`~spark_pulse.tools.backends.base.RunBackend` over ``native_runtime``."""

    name = "docker"

    def plan(
        self,
        recipe_id: str,
        *,
        engine: str | None = None,
        variant: str | None = None,
        model: str | None = None,
        params: dict[str, Any] | None = None,
        extra_args: list[str] | None = None,
        nodes: list[str] | None = None,
        solo: bool = True,
        allow_missing_model: bool = True,
    ) -> Any:
        return tools.native_runtime.plan(
            recipe_id,
            engine=engine,
            variant=variant,
            model=model,
            params=params or {},
            extra_args=extra_args or [],
            nodes=nodes,
            solo=solo,
            allow_missing_model=allow_missing_model,
        )

    def start(
        self,
        recipe_id: str,
        name: str,
        params: dict[str, Any],
        *,
        nodes: list[str] | None = None,
        engine: str | None = None,
        variant: str | None = None,
        model: str | None = None,
        extra_args: list[str] | None = None,
        allow_missing_model: bool = False,
    ) -> dict[str, Any]:
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

    def status(self, run_id: str) -> dict[str, Any] | None:
        return tools.native_runtime.status(run_id)

    def stop(self, run_id: str) -> dict[str, Any] | None:
        return tools.native_runtime.stop_deployment(run_id)

    def delete(self, run_id: str) -> bool:
        return tools.native_runtime.delete_deployment(run_id)

    def logs(self, run_id: str, lines: int = 200) -> str:
        return tools.native_runtime.get_logs(run_id, lines)

    def list_runs(self) -> list[dict[str, Any]]:
        return tools.native_runtime.list_deployments()
