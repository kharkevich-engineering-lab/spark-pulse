"""SGLang engine plugin.

SGLang has no ``command`` template concept: the launch line is always built
from engine-neutral params mapped through ``param_flags``, and the rendezvous
flags (``--nnodes/--node-rank/--dist-init-addr``) are passed even for a single
node, matching the hardware-verified invocation in section 1.5 of the plan.

Unlike vLLM, SGLang honours ``--dist-init-addr`` at one node too, so at one
node it is pointed at loopback rather than at the fabric address: loopback is
both correct there and robust when the fabric link is down or unaddressed.
"""

from __future__ import annotations

from typing import Any

from spark_pulse.engines.base import (
    Engine,
    EngineError,
    LaunchScript,
    Topology,
)

# Flag order for the rendered command line.
_PARAM_ORDER = (
    "tensor_parallel",
    "pipeline_parallel",
    "host",
    "port",
    "gpu_memory_utilization",
    "max_model_len",
)

_DEFAULTS: dict[str, Any] = {
    "tensor_parallel": 1,
    "pipeline_parallel": 1,
    "host": "0.0.0.0",
    "gpu_memory_utilization": 0.9,
}


class SglangEngine(Engine):
    """SGLang on DGX Spark."""

    name = "sglang"

    def base_env(
        self,
        node_ip: str = "",
        eth_if: str = "",
        ib_if: str = "",
        node_count: int = 1,
    ) -> dict[str, str]:
        env: dict[str, str] = dict(self.spec.runtime.env)
        env.update(self.pinning_env(eth_if, ib_if, node_count))
        return env

    # ``supports`` is the base implementation: a top-level ``command`` is
    # written in another engine's flags and pins the recipe to it, while a bare
    # ``engine:`` only names the default engine.

    # -- rendering ---------------------------------------------------------

    def render(
        self,
        recipe: dict[str, Any],
        model: str | None = None,
        params: dict[str, Any] | None = None,
        extra_args: list[str] | None = None,
        topology: Topology | None = None,
        node_rank: int = 0,
    ) -> LaunchScript:
        ok, reason = self.supports(recipe)
        if not ok:
            raise EngineError(reason)

        topology = topology or Topology.solo()
        if node_rank < 0 or node_rank >= topology.size:
            raise EngineError(
                f"node_rank {node_rank} is out of range for {topology.size} node(s)"
            )

        overrides = {k: v for k, v in (params or {}).items() if v is not None}
        resolved = self._resolved_params(recipe, overrides)
        for key, value in _DEFAULTS.items():
            resolved.setdefault(key, value)
        resolved.setdefault("port", self.api_port())

        resolved_model = self._resolved_model(recipe, model)
        serve = self.spec.runtime.serve or "python3 -m sglang.launch_server"
        model_arg = self.spec.runtime.model_arg or "--model-path"

        parts = [serve]
        if model_arg == "positional":
            parts.append(resolved_model)
        else:
            parts.extend([model_arg, resolved_model])
        parts.extend(self._flag_args(resolved, order=_PARAM_ORDER))

        # SGLang reads this address even at one node, where the fabric is not
        # involved at all — so point it at loopback there.
        dist_addr = "127.0.0.1" if topology.size == 1 else topology.head.address()
        rendezvous = self.rendezvous_port() or 50000
        parts.extend(
            [
                "--nnodes",
                str(topology.size),
                "--node-rank",
                str(node_rank),
                "--dist-init-addr",
                f"{dist_addr}:{rendezvous}",
            ]
        )

        if topology.size > 1:
            parts.extend(self.spec.runtime.multi_node.extra_args)

        args = self._engine_args(recipe)
        if args:
            parts.append(args)

        tail = self._quote_extra(extra_args)
        if tail:
            parts.append(tail)

        command = " ".join(p for p in parts if p).strip()

        # The topology is total and the rank is checked above, so there is
        # always a node here.
        node = topology.nodes[node_rank]
        env = self.base_env(
            node_ip=node.ip,
            eth_if=node.eth_if,
            ib_if=node.ib_if,
            node_count=topology.size,
        )
        env.update(self._block_env(recipe))

        return LaunchScript(
            node_rank=node_rank,
            host=node.host,
            command=command,
            env=env,
            script=self._script(env, command),
        )

    def _engine_args(self, recipe: dict[str, Any]) -> str:
        return " ".join(self._block_args(recipe).split())
