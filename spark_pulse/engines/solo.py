"""Engines that are one process on one machine.

vLLM and SGLang each need a class of their own because each has its own
rendezvous dialect: ``--nnodes/--node-rank/--master-addr/--master-port`` for
one, ``--dist-init-addr`` for the other, rendered at every size so there is no
solo special case left to get wrong.

These five have no rendezvous at all, and that is what makes one class enough
for all of them:

* **llama.cpp** can spread over its RPC backend, which upstream still calls
  experimental;
* **TensorRT-LLM** goes multi-node as an MPI job, and NVIDIA's image for this
  hardware says ``single-gpu`` in its own tag;
* **Modular MAX** has no distributed tensor parallelism whatsoever — it takes
  more *local* GPUs with ``--devices``, and a Spark has one;
* **Atlas** bootstraps its own NCCL world with
  ``--rank/--world-size/--master-addr/--master-port``, a third dialect that
  nothing here renders yet.

So the launch is the serve command, the model, and the engine-neutral params
mapped through ``param_flags`` — all of it read from the engine spec, none of
it written here. A class per engine exists only to carry the name the registry
looks up and, where the engine wants one, a default a recipe did not give.

The single-node claim is not this module's opinion: every one of these specs
declares ``cluster: false``, so :meth:`Engine.supports_size` refuses a larger
topology long before rendering. :meth:`SoloEngine.render` refuses one too,
because a renderer that quietly produced a one-node command for a two-node
deployment would be the worse failure.
"""

from __future__ import annotations

from typing import Any

from spark_pulse.engines.base import (
    Engine,
    EngineError,
    LaunchScript,
    Topology,
)

#: Order the mapped flags appear in. Readability only — a serve command means
#: the same thing in any order — but a launch command an operator has to read
#: in a log is worth keeping in a familiar shape.
_PARAM_ORDER = (
    "host",
    "port",
    "tensor_parallel",
    "pipeline_parallel",
    "gpu_memory_utilization",
    "max_model_len",
    "max_num_batched_tokens",
    "max_num_seqs",
)


class SoloEngine(Engine):
    """One process, one machine, rendered from the spec alone."""

    #: Params a recipe need not carry. ``port`` is not here: it comes from the
    #: spec's own API port, which differs per engine.
    defaults: dict[str, Any] = {"host": "0.0.0.0"}

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
        if topology.size > 1:
            raise EngineError(
                f"engine '{self.spec.key}' runs on one node: it has no "
                f"rendezvous to render for {topology.size}. Deploy it solo, "
                "or pick an engine that declares cluster support"
            )
        if node_rank != 0:
            raise EngineError(
                f"node_rank {node_rank} is out of range for a single-node engine"
            )

        overrides = {k: v for k, v in (params or {}).items() if v is not None}
        resolved = self._resolved_params(recipe, overrides)
        for key, value in self.defaults.items():
            resolved.setdefault(key, value)
        resolved.setdefault("port", self.api_port())

        resolved_model = self._resolved_model(recipe, model)
        serve = self.spec.runtime.serve
        if not serve:
            raise EngineError(
                f"engine '{self.spec.key}' declares no serve command, so there "
                "is nothing to launch"
            )
        model_arg = self.spec.runtime.model_arg or "positional"

        parts = [serve]
        if model_arg == "positional":
            parts.append(resolved_model)
        else:
            parts.extend([model_arg, resolved_model])
        parts.extend(self._flag_args(resolved, order=_PARAM_ORDER))

        args = " ".join(self._block_args(recipe).split())
        if args:
            parts.append(args)

        tail = self._quote_extra(extra_args)
        if tail:
            parts.append(tail)

        command = " ".join(p for p in parts if p).strip()

        node = topology.nodes[0]
        env = self.base_env(
            node_ip=node.ip,
            eth_if=node.eth_if,
            ib_if=node.ib_if,
            node_count=topology.size,
            mesh=node.mesh,
        )
        env.update(self._block_env(recipe))

        return LaunchScript(
            node_rank=0,
            host=node.host,
            command=command,
            env=env,
            script=self._script(env, command),
        )


class LlamaCppEngine(SoloEngine):
    """llama.cpp's ``llama-server``.

    The one engine here that does not read the model the others read:
    llama.cpp loads GGUF, so the model must be a GGUF repository and ``-hf``
    is what resolves it.
    """

    name = "llama-cpp"


class TrtllmEngine(SoloEngine):
    """TensorRT-LLM's ``trtllm-serve``."""

    name = "trtllm"


class ModularMaxEngine(SoloEngine):
    """Modular MAX's ``max serve``."""

    name = "modular-max"


class AtlasEngine(SoloEngine):
    """Atlas's ``spark serve``."""

    name = "atlas"
