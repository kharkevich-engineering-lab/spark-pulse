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

import importlib
from typing import TYPE_CHECKING, Any, Callable

from spark_pulse import tools
from spark_pulse.tools.backends import runtime_checks
from spark_pulse.tools.backends.runtime_checks import RuntimeReport

if TYPE_CHECKING:
    from spark_pulse.tools.intent import RunIntent


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

    def realise(
        self, intent: RunIntent, services: Callable[[str], Any] | None = None
    ) -> Any:
        return tools.native_runtime.realise(intent, services=services)

    def model_location(
        self,
        intent: RunIntent,
        node: str,
        services: Callable[[str], Any],
        warnings: list[str],
    ) -> tuple[str, str]:
        # The real module, not the switch: the hook is private and the mock
        # re-exports only the public surface. ``from spark_pulse.tools import
        # native_runtime`` would hand back the mock under SIMULATION_MODE;
        # simulation swaps the agent transport underneath this call anyway.
        runtime = importlib.import_module("spark_pulse.tools.native_runtime")
        return runtime._resolve_model_file(
            intent.engine,
            intent.recipe,
            intent.model,
            intent.model_present,
            node,
            services,
            warnings,
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

    def diagnose(
        self,
        node_id: str,
        label: str = "",
        run: Callable[[str], Any] | None = None,
        facts: Any = None,
    ) -> RuntimeReport:
        """The node's runtime, checked by the runtime its agent reports.

        Docker and Podman are one backend — the same lifecycle, with Podman
        differing only at create, which the agent decides (#165, #167) — so
        the choice of checks is per node, from ``container_runtime``.
        """
        reported = (
            runtime_checks.RuntimeFacts.from_node_facts(facts)
            if facts is not None
            else _reported_facts(node_id)
        )
        checks = runtime_checks.checks_for(reported)
        return runtime_checks.run_checks(checks, label or node_id, reported, run)


def _reported_facts(node_id: str) -> runtime_checks.RuntimeFacts:
    """What the node's agent last reported, or that nothing is connected."""
    from spark_pulse.agent import runtime as agent_runtime

    current = agent_runtime.current()
    connection = current.hub.get(node_id) if current is not None and node_id else None
    if connection is None:
        return runtime_checks.RuntimeFacts()
    return runtime_checks.RuntimeFacts.from_node_facts(connection.facts)
