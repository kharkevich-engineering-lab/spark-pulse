"""SGLang engine plugin.

SGLang has no ``command`` template concept: the launch line is always built
from engine-neutral params mapped through ``param_flags``, and the rendezvous
flags (``--nnodes/--node-rank/--dist-init-addr``) are passed even for a single
node, matching the hardware-verified invocation in section 1.5 of the plan.
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
    ) -> dict[str, str]:
        env: dict[str, str] = dict(self.spec.runtime.env)
        if eth_if:
            env["NCCL_SOCKET_IFNAME"] = eth_if
            env["GLOO_SOCKET_IFNAME"] = eth_if
        if ib_if:
            env["NCCL_IB_HCA"] = ib_if
        return env

    def supports(self, recipe: dict[str, Any]) -> tuple[bool, str]:
        engine = recipe.get("engine")
        if engine and engine != self.name:
            return False, f"recipe pins engine '{engine}'"
        if str(recipe.get("command") or "").strip():
            return (
                False,
                (
                    "recipe carries an engine-specific command "
                    "(vLLM v1 flags cannot run on SGLang)"
                ),
            )
        engines = recipe.get("engines")
        if isinstance(engines, dict) and engines and self.name not in engines:
            listed = ", ".join(sorted(engines))
            return False, f"recipe only declares engines: {listed}"
        return True, ""

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

        head = topology.head
        dist_addr = head.address() if head else "127.0.0.1"
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

        node = topology.node(node_rank)
        env = self.base_env(
            node_ip=node.ip if node else "",
            eth_if=node.eth_if if node else "",
            ib_if=node.ib_if if node else "",
        )
        env.update({str(k): str(v) for k, v in (recipe.get("env") or {}).items()})

        return LaunchScript(
            node_rank=node_rank,
            host=node.host if node else "",
            command=command,
            env=env,
            script=self._script(env, command),
        )

    def _engine_args(self, recipe: dict[str, Any]) -> str:
        engines = recipe.get("engines")
        if isinstance(engines, dict):
            block = engines.get(self.name)
            if isinstance(block, dict) and block.get("args"):
                return " ".join(str(block["args"]).split())
        if recipe.get("args"):
            return " ".join(str(recipe["args"]).split())
        return ""
