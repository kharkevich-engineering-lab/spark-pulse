"""vLLM engine plugin.

For v1 recipes (those carrying a ``command:`` template) the rendering
reproduces upstream ``run-recipe.py`` semantics exactly:

* params are the recipe ``defaults`` merged with caller overrides;
* the command is formatted with :meth:`str.format`, so ``{{`` / ``}}`` are
  literal braces and a missing key is a hard, explained error;
* trailing backslash line continuations are folded into a single line;
* extra args are appended ``shlex``-quoted;
* ``--distributed-executor-backend`` never survives: there is no Ray at any
  size, and vLLM picks ``uni`` at one node and ``mp`` above on its own;
* every size gets ``--nnodes/--node-rank/--master-addr/--master-port``, plus
  ``--headless`` for every rank above zero. These are stock ``vllm serve``
  flags since 0.11.1 (`PR #23691
  <https://github.com/vllm-project/vllm/pull/23691>`_). Rendering them
  unconditionally is what removes the solo special case — there is no rewrite
  left to get wrong.

  The precise claim about one node, because a looser one was wrong here
  before: the executor chooses a ``file://`` store *unconditionally*, and
  ``init_distributed_environment`` overrides it with
  ``tcp://master_addr:master_port`` only when ``nnodes > 1`` **or**
  ``data_parallel_size > 1``. So at ``nnodes=1`` with ``dp=1`` the two address
  flags are not read, and at ``nnodes=1`` with ``dp>1`` ``--master-addr``
  *is*: ``create_engine_config`` seeds the data-parallel address from it. We
  render loopback there, which is the value that path would have used anyway.

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

    # ``--nnodes/--node-rank/--master-addr/--master-port`` and ``--headless``
    # became stock ``vllm serve`` flags in 0.11.1. An older image would not
    # understand them, and this renderer emits them at every size.
    min_framework_version = (0, 11, 1)

    def base_env(
        self,
        node_ip: str = "",
        eth_if: str = "",
        ib_if: str = "",
        node_count: int = 1,
        mesh: bool = False,
    ) -> dict[str, str]:
        env: dict[str, str] = {}
        if node_ip:
            env["VLLM_HOST_IP"] = node_ip
        # A no-op: NCCL's own default is 0 (``NCCL_PARAM(IbDisable, …, 0)``,
        # undocumented in the env guide) and the only use site is a ``!= 0``
        # early return. Upstream sets it explicitly at launch-cluster.sh:967
        # and so do we, because an operator reading the rendered environment
        # should be able to see that RoCE is meant to be on.
        env["NCCL_IB_DISABLE"] = "0"
        env.update(self.pinning_env(eth_if, ib_if, node_count, mesh))
        env.update(self.spec.runtime.env)
        return env

    def _fabric_env(self, eth_if: str, ib_if: str) -> dict[str, str]:
        """Upstream's per-node interface variables, verbatim.

        ``launch-cluster.sh`` lines 957-974 set all of these. Three of them do
        nothing in *our* launch and are kept for parity rather than effect:
        ``UCX_NET_DEVICES`` is read by libucp when a UCX context is created,
        ``OMPI_MCA_btl_tcp_if_include`` by Open MPI inside ``MPI_Init``, and
        ``TP_SOCKET_IFNAME`` by PyTorch's TensorPipe RPC agent — none of which
        a ``vllm serve`` over NCCL and Gloo ever constructs. Upstream needs
        them because the same interface names drive its ``mpirun``-based
        ``nccl-tests`` runs. They cost nothing, they are correct if anything
        ever does load UCX (the ``ucc`` process-group backend would), and
        dropping them would be a divergence with no benefit.

        ``MN_IF_NAME`` is likewise not read by NCCL, Gloo or vLLM; it is
        upstream's own convention and is kept for the same reason.
        """
        env: dict[str, str] = {}
        if eth_if:
            env["MN_IF_NAME"] = eth_if
            env["UCX_NET_DEVICES"] = eth_if
            env["NCCL_SOCKET_IFNAME"] = eth_if
            env["OMPI_MCA_btl_tcp_if_include"] = eth_if
            env["GLOO_SOCKET_IFNAME"] = eth_if
            env["TP_SOCKET_IFNAME"] = eth_if
        if ib_if:
            env["NCCL_IB_HCA"] = ib_if
        return env

    # ``supports`` is the base implementation; a v1 ``command`` template is
    # vLLM's own, so it never refuses one.

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

        template = recipe.get("command") or ""
        if template.strip():
            command = format_command(fold_continuations(template), resolved)
        else:
            command = self._render_v2(recipe, model, resolved)

        # No Ray at any size: the backend flag never survives, and vLLM
        # resolves its own executor (``uni`` at one node, ``mp`` above).
        command = strip_distributed_executor_backend(command)
        command = f"{command} {self._rendezvous_args(topology, node_rank)}"

        tail = self._quote_extra(extra_args)
        if tail:
            command = f"{command} {tail}"
        command = command.strip()

        # The topology is total and the rank is checked above, so there is
        # always a node here.
        node = topology.nodes[node_rank]
        env = self.base_env(
            node_ip=node.ip,
            eth_if=node.eth_if,
            ib_if=node.ib_if,
            node_count=topology.size,
            mesh=node.mesh,
        )
        env.update(self._block_env(recipe))

        return LaunchScript(
            node_rank=node_rank,
            host=node.host,
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
        return self._block_args(recipe)

    def _rendezvous_args(self, topology: Topology, node_rank: int) -> str:
        """Rendezvous flags, rendered identically at every topology size."""
        master_addr = topology.head.address()
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
