"""llama.cpp — one process on one machine, or an RPC gang across several.

``llama-server`` is a solo engine by default and this class renders it as one:
the bundled ``llama-cpp`` spec declares ``cluster: false`` and
``multi_node: {style: none}``, so nothing about that engine changed. It is the
one engine that does not read the model the others read — llama.cpp loads
GGUF, so the model must be a GGUF repository and ``-hf`` is what resolves it.

What is new is the other style. llama.cpp spans machines through its **RPC
backend** (upstream ``tools/rpc/README.md``): a ``ggml-rpc-server`` runs on
every worker, the head is given ``--rpc host:port[,host:port]`` naming all of
them, and the head — the only rank that loads the model — offloads layers onto
those devices. A variant whose ``engine.yaml`` declares
``multi_node: {style: llama-rpc}`` together with ``capabilities.cluster: true``
is rendered that way; ``llama-cpp/prism`` is the first that does, because
PrismML's fork is the only build that runs their ternary Bonsai GGUFs and a
27B ternary model is what an operator wants two Sparks for.

Three consequences, all of them deliberate:

* **There is no rendezvous, so the order matters more.** No NCCL, no Gloo, no
  store to form: the head opens a TCP connection to each worker *at load time*
  and fails outright if nothing is listening. The runtime already launches in
  the order this needs — every container created first, then workers launched
  and rank zero last (``native_runtime.DeployPlan.start_order``), which is
  upstream's order for the rendezvous engines and exactly the guarantee an RPC
  client wants.
* **A worker serves no HTTP.** ``ggml-rpc-server`` speaks the RPC protocol and
  nothing else, so a worker answers no readiness endpoint. That is the shape a
  headless vLLM worker already has and the runtime reads it the same way: only
  rank zero's readiness URL is polled, and every other rank is watched for
  having exited (``native_runtime._wait_ready``). No new readiness path.
* **The launch states no parallelism.** There is no ``-tp`` to parse: the
  shape *is* the ``--rpc`` list, one server per worker, so the gang occupies
  exactly the nodes it was planned across. Hence
  :attr:`Engine.parallelism_in_command` is false here.
"""

from __future__ import annotations

from typing import Any

from spark_pulse.engines.base import (
    EngineError,
    LaunchScript,
    Topology,
)
from spark_pulse.engines.solo import SoloEngine

#: The ``multi_node.style`` a spec declares to be rendered as an RPC gang.
RPC_STYLE = "llama-rpc"

#: What a worker runs. ``ggml-rpc-server`` is llama.cpp's own name for the
#: binary — at the upstream pin and at the prism ref alike — and the image
#: ships a ``rpc-server`` symlink beside it because upstream's README still
#: writes that. The real name is rendered: a symlink is a courtesy, and a
#: launch that depends on one breaks the first time an image drops it.
RPC_SERVER = "ggml-rpc-server"

#: Port a worker listens on when the spec names none — the one every
#: example in upstream's README uses.
DEFAULT_RPC_PORT = 50052


class LlamaCppEngine(SoloEngine):
    """llama.cpp's ``llama-server``, solo or over the RPC backend."""

    name = "llama-cpp"

    # The head's command carries no -tp/-pp/-dp: see the module docstring.
    parallelism_in_command = False

    # -- what this variant claims ------------------------------------------

    def spans_nodes(self) -> bool:
        """Whether this spec asks to be rendered as an RPC gang."""
        return self.spec.runtime.multi_node.style == RPC_STYLE

    def rpc_port(self) -> int | None:
        """The port each worker binds, or None when this variant is solo.

        Declared by the spec as ``runtime.ports.rpc``. A variant that names
        the style but no port gets :data:`DEFAULT_RPC_PORT`, which is what
        upstream's own examples bind.
        """
        if not self.spans_nodes():
            return None
        return int(self.spec.runtime.ports.rpc or DEFAULT_RPC_PORT)

    def supports_size(self, node_count: int) -> tuple[bool, str]:
        """The capability flags, plus the one thing they cannot say.

        ``capabilities.cluster`` is still what decides — the bundled
        ``llama-cpp`` spec declares it false and is refused above one node by
        the base implementation, unchanged. What is added is the other half:
        a spec that claims the cluster but declares no ``llama-rpc`` style has
        nothing for this renderer to emit, and saying so here means the
        refusal arrives at plan time with the fix in it rather than as a
        renderer complaining about a node count.
        """
        ok, reason = super().supports_size(node_count)
        if not ok or node_count <= 1:
            return ok, reason
        if not self.spans_nodes():
            return False, (
                f"engine '{self.spec.key}' declares cluster support but no "
                f"multi-node style, so there is nothing to render for "
                f"{node_count} nodes: llama.cpp spans machines only through "
                f"its RPC backend, which a spec asks for with "
                f"multi_node: {{style: {RPC_STYLE}}}"
            )
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
        if topology.size == 1:
            # One node is one llama-server, whichever style the spec names.
            return super().render(
                recipe,
                model=model,
                params=params,
                extra_args=extra_args,
                topology=topology,
                node_rank=node_rank,
            )

        # Above one node the claim decides, and it is the same claim the plan
        # already checked — asked again here because a renderer that produced
        # a one-node command for a two-node deployment would be the worse
        # failure, and because the reason is worth repeating verbatim.
        size_ok, size_reason = self.supports_size(topology.size)
        if not size_ok:
            raise EngineError(size_reason)
        if node_rank < 0 or node_rank >= topology.size:
            raise EngineError(
                f"node_rank {node_rank} is out of range for {topology.size} node(s)"
            )
        if node_rank > 0:
            return self._worker_launch(recipe, topology, node_rank)
        return self._head_launch(recipe, model, params, extra_args, topology)

    def _head_launch(
        self,
        recipe: dict[str, Any],
        model: str | None,
        params: dict[str, Any] | None,
        extra_args: list[str] | None,
        topology: Topology,
    ) -> LaunchScript:
        """Rank zero: the solo command plus one ``--rpc`` endpoint per worker.

        The addresses are the ones every rank is named by — the registered
        address, the same one vLLM's ``--master-addr`` carries. Worth being
        explicit that this is not the fastest wire available: what crosses it
        is tensor traffic, which is bulk bytes by any measure, and
        ``node_service.transfer_route`` already picks a node's ConnectX
        address over its management one for exactly that reason. It is not
        consulted here — the launch names nodes the way every other launch
        does — and moving it would be its own change, with its own evidence:
        a fabric address that is reachable for an rsync is not yet proof that
        the head can hold an RPC session open on it for the life of a run.
        """
        port = self.rpc_port()
        endpoints = ",".join(f"{n.address()}:{port}" for n in topology.nodes[1:])
        tail = ["--rpc", endpoints, *self.spec.runtime.multi_node.extra_args]
        return self._serve_launch(
            recipe,
            model=model,
            params=params,
            extra_args=extra_args,
            topology=topology,
            node_rank=0,
            tail=tail,
        )

    def _worker_launch(
        self, recipe: dict[str, Any], topology: Topology, node_rank: int
    ) -> LaunchScript:
        """A worker: the RPC server on every interface, and nothing else.

        No model, no serve flags and no extra args — none of them are
        ``ggml-rpc-server`` arguments, and a worker that took the head's flags
        would exit on the first one it does not know. ``-H 0.0.0.0`` because
        the head connects from another machine; the container is on the host
        network, so that is the node's own address.
        """
        node = topology.nodes[node_rank]
        command = f"{RPC_SERVER} -H 0.0.0.0 -p {self.rpc_port()}"
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
