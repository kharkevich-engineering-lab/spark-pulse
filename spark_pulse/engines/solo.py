"""Engines that are one process on one machine.

vLLM and SGLang each need a class of their own because each has its own
rendezvous dialect: ``--nnodes/--node-rank/--master-addr/--master-port`` for
one, ``--dist-init-addr`` for the other, rendered at every size so there is no
solo special case left to get wrong.

These four have no rendezvous at all, and that is what makes one class enough
for all of them:

* **TensorRT-LLM** goes multi-node as an MPI job, and NVIDIA's image for this
  hardware says ``single-gpu`` in its own tag;
* **Modular MAX** has no distributed tensor parallelism whatsoever — it takes
  more *local* GPUs with ``--devices``, and a Spark has one;
* **Atlas** bootstraps its own NCCL world with
  ``--rank/--world-size/--master-addr/--master-port``, a third dialect that
  nothing here renders yet;
* **llama.cpp** is served by a subclass of its own
  (:mod:`spark_pulse.engines.llama_cpp`) because it is no longer only this:
  it spans machines through its RPC backend, with one ``ggml-rpc-server``
  per worker instead of a rendezvous. A variant that does not declare that style
  renders here, as one process on one machine, exactly as before.

So the launch is the serve command, the model, and the engine-neutral params
mapped through ``param_flags`` — all of it read from the engine spec, none of
it written here. A class per engine exists only to carry the name the registry
looks up and, where the engine wants one, a default a recipe did not give.

The single-node claim is not this module's opinion: each of these specs
declares ``cluster: false``, so :meth:`Engine.supports_size` refuses a larger
topology long before rendering. :meth:`SoloEngine.render` refuses one too,
because a renderer that quietly produced a one-node command for a two-node
deployment would be the worse failure. :meth:`SoloEngine._serve_launch` is
the part below that refusal, so an engine that *can* render a rank above one
node builds the same serve line rather than a second copy of it.
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
        model_file: str = "",
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
        return self._serve_launch(
            recipe, model, params, extra_args, topology, 0, model_file=model_file
        )

    def _serve_launch(
        self,
        recipe: dict[str, Any],
        model: str | None = None,
        params: dict[str, Any] | None = None,
        extra_args: list[str] | None = None,
        topology: Topology | None = None,
        node_rank: int = 0,
        tail: list[str] | None = None,
        model_file: str = "",
    ) -> LaunchScript:
        """The serve line for one rank, with nothing refused.

        Split out of :meth:`render` so a subclass that renders above one node
        — llama.cpp's RPC head — builds the same command rather than its own
        near-copy. ``tail`` lands after the mapped params and before the
        recipe's own arguments, so what an operator wrote still comes last.

        ``model_file`` is a path the control plane resolved inside the mounted
        cache; :meth:`_model_parts` and :meth:`_recipe_args` are where an
        engine that can use one says what it changes.
        """
        topology = topology or Topology.solo()
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
        parts = [serve]
        parts.extend(self._model_parts(resolved_model, model_file))
        parts.extend(self._flag_args(resolved, order=_PARAM_ORDER))
        parts.extend(tail or [])

        args = self._recipe_args(recipe, model_file)
        if args:
            parts.append(args)

        tail = self._quote_extra(extra_args)
        if tail:
            parts.append(tail)

        command = " ".join(p for p in parts if p).strip()

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

    # -- the two halves a resolved model file changes -----------------------

    def _model_parts(self, resolved_model: str, model_file: str) -> list[str]:
        """How this engine names the model on its serve line.

        Read from the spec, which is the whole point: ``model_arg`` says
        whether the model is positional or takes a flag, and nothing about it
        is written per engine. ``model_file`` is ignored here — an engine that
        can be handed a path says so by overriding this.
        """
        model_arg = self.spec.runtime.model_arg or "positional"
        if model_arg == "positional":
            return [resolved_model]
        return [model_arg, resolved_model]

    def _recipe_args(self, recipe: dict[str, Any], model_file: str) -> str:
        """The recipe's own argument tail, normalised to one line."""
        return " ".join(self._block_args(recipe).split())


class TrtllmEngine(SoloEngine):
    """TensorRT-LLM's ``trtllm-serve``."""

    name = "trtllm"


class ModularMaxEngine(SoloEngine):
    """Modular MAX's ``max serve``."""

    name = "modular-max"


class AtlasEngine(SoloEngine):
    """Atlas's ``spark serve``."""

    name = "atlas"
