"""vLLM engine plugin.

For v1 recipes (those carrying a ``command:`` template) the rendering
reproduces upstream ``run-recipe.py`` semantics exactly:

* params are the recipe ``defaults`` merged with caller overrides;
* the command is formatted with :meth:`str.format`, so ``{{`` / ``}}`` are
  literal braces and a missing key is a hard, explained error;
* trailing backslash line continuations are folded into a single line;
* extra args are appended ``shlex``-quoted;
* solo forces ``tensor_parallel=1`` unless explicitly overridden and strips
  ``--distributed-executor-backend``;
* multi-node (no Ray) strips it too and appends
  ``--nnodes/--node-rank/--master-addr/--master-port``, plus ``--headless``
  for every rank above zero.

Recipes without a ``command`` (v2 style) render
``vllm serve <model> <mapped params> <engine args>``.
"""

from __future__ import annotations

import re
from typing import Any

from spark_pulse.engines.base import (
    Engine,
    EngineError,
    LaunchScript,
    Topology,
)

_DEB_RE = re.compile(r"\s*--distributed-executor-backend(?:=|\s+)\S+")

# Order in which engine-neutral params become flags for a v2 render.
_PARAM_ORDER = (
    "model",
    "host",
    "port",
    "tensor_parallel",
    "pipeline_parallel",
    "gpu_memory_utilization",
    "max_model_len",
    "max_num_batched_tokens",
    "max_num_seqs",
)


def fold_continuations(command: str) -> str:
    """Fold a multi-line shell command into one line, dropping trailing ``\\``."""
    parts: list[str] = []
    for raw_line in command.splitlines():
        line = raw_line.strip()
        while line.endswith("\\"):
            line = line[:-1].rstrip()
        if line:
            parts.append(line)
    return " ".join(parts)


def strip_distributed_executor_backend(command: str) -> str:
    """Remove any ``--distributed-executor-backend <value>`` from *command*."""
    return _DEB_RE.sub("", command).strip()


def format_command(template: str, params: dict[str, Any]) -> str:
    """``str.format`` the template, turning failures into clear errors."""
    try:
        return template.format(**params)
    except KeyError as exc:
        missing = exc.args[0] if exc.args else "?"
        available = ", ".join(sorted(str(k) for k in params)) or "none"
        raise EngineError(
            f"recipe command references unknown placeholder "
            f"{{{missing}}}; available params: {available}"
        ) from exc
    except IndexError as exc:
        raise EngineError(
            "recipe command uses a positional placeholder {}; "
            "use named placeholders such as {port}"
        ) from exc
    except ValueError as exc:
        raise EngineError(
            f"recipe command is not a valid format string: {exc}"
        ) from exc


class VllmEngine(Engine):
    """vLLM on DGX Spark."""

    name = "vllm"

    def base_env(
        self,
        node_ip: str = "",
        eth_if: str = "",
        ib_if: str = "",
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        if node_ip:
            env["VLLM_HOST_IP"] = node_ip
        if eth_if:
            env["MN_IF_NAME"] = eth_if
            env["UCX_NET_DEVICES"] = eth_if
            env["NCCL_SOCKET_IFNAME"] = eth_if
        if ib_if:
            env["NCCL_IB_HCA"] = ib_if
        env["NCCL_IB_DISABLE"] = "0"
        if eth_if:
            env["OMPI_MCA_btl_tcp_if_include"] = eth_if
            env["GLOO_SOCKET_IFNAME"] = eth_if
            env["TP_SOCKET_IFNAME"] = eth_if
        env.update(self.spec.runtime.env)
        return env

    def supports(self, recipe: dict[str, Any]) -> tuple[bool, str]:
        engine = recipe.get("engine")
        if engine and engine != self.name:
            return False, f"recipe pins engine '{engine}'"
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

        # Solo forces tp=1 unless the caller explicitly asked for something.
        if topology.is_solo and "tensor_parallel" not in overrides:
            resolved["tensor_parallel"] = 1

        template = recipe.get("command") or ""
        if template.strip():
            command = format_command(fold_continuations(template), resolved)
        else:
            command = self._render_v2(recipe, model, resolved)

        # No Ray in either topology: the backend flag never survives.
        command = strip_distributed_executor_backend(command)

        if topology.size > 1:
            command = f"{command} {self._multi_node_args(topology, node_rank)}"

        tail = self._quote_extra(extra_args)
        if tail:
            command = f"{command} {tail}"
        command = command.strip()

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

    def _render_v2(
        self, recipe: dict[str, Any], model: str | None, params: dict[str, Any]
    ) -> str:
        resolved_model = self._resolved_model(recipe, model)
        parts = [self.spec.runtime.serve or "vllm serve", resolved_model]
        parts.extend(self._flag_args(params, order=_PARAM_ORDER))

        args = self._engine_args(recipe)
        if args:
            parts.append(format_command(fold_continuations(args), params))
        return " ".join(parts)

    def _engine_args(self, recipe: dict[str, Any]) -> str:
        engines = recipe.get("engines")
        if isinstance(engines, dict):
            block = engines.get(self.name)
            if isinstance(block, dict) and block.get("args"):
                return str(block["args"])
        if recipe.get("args"):
            return str(recipe["args"])
        return ""

    def _multi_node_args(self, topology: Topology, node_rank: int) -> str:
        head = topology.head
        master_addr = head.address() if head else "127.0.0.1"
        master_port = self.rendezvous_port() or 29501
        args = [
            "--nnodes",
            str(topology.size),
            "--node-rank",
            str(node_rank),
            "--master-addr",
            master_addr,
            "--master-port",
            str(master_port),
        ]
        if node_rank > 0:
            args.append("--headless")
        return " ".join(args)
