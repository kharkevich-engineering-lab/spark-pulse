"""What to run, before anything decides how: a run's backend-neutral intent.

:func:`resolve_intent` answers everything a run is *about* — the recipe, the
engine and variant, the image, the model and whether the catalogue holds it,
the nodes and their fabric, the ports the engine binds itself and the API port
the caller pinned or the recipe would like — and refuses what cannot run on
any backend. :class:`RunIntent` carries the answer.

How it runs is the backend's (``tools.backends``): which port it actually gets
on nodes already serving something, where the model file sits inside whatever
the engine runs in, containers, mounts, labels, the memory budget. The rendered
engine command depends on two of those — the chosen port and the model's path
— so the intent renders on request (:meth:`RunIntent.render`), once the backend
has decided them.

Nothing here is about Docker; ``tests/test_run_intent.py`` holds that to the
imports.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.engines import (
    Engine,
    EngineError,
    EngineNotFound,
    NodeInfo,
    Topology,
)
from spark_pulse.tools.discovery import FABRIC_MESH, MESH_RING_NODES
from spark_pulse.tools.parallelism import (
    ClusterCapacity,
    parse_parallelism,
    validate_cluster_capacity,
)
from spark_pulse.tools.redaction import REDACTED, is_secret_env
from spark_pulse.tools.runtime_errors import MissingModelError, NativeRuntimeError

#: Recipe keys that are computed against this control plane when a recipe is
#: listed — which engines are enabled, which support it — rather than written
#: in the recipe. They would change a run's hash without changing the run.
RECIPE_ANNOTATIONS = frozenset({"engine_support"})


def _redacted(value: Any) -> Any:
    """``value`` with every credential-named key's value replaced.

    The intent is stored on the run's record, which a browser reads, and a
    recipe's ``env:`` may name a token. The rule is the plan's own
    (:func:`~spark_pulse.tools.redaction.is_secret_env`), applied at any depth.
    """
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_secret_env(str(k)) else _redacted(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redacted(v) for v in value]
    return value


@dataclass
class RunIntent:
    """What a run is, before a backend decides how it runs."""

    #: The id the caller asked for; ``recipe["id"]`` may differ (an alias).
    recipe_id: str
    recipe: dict[str, Any]
    engine: Engine
    engine_name: str
    variant: str
    image_ref: str
    #: The resolved model id, empty for a v1 recipe whose command embeds it.
    model: str
    #: What the caller passed as ``model``, which is what the engine renders.
    model_requested: str | None
    model_present: bool
    topology: Topology
    #: The node addresses asked for; empty is this machine.
    nodes: list[str]
    mods: list[str]
    #: The caller's own params, ``None`` values dropped. The engine merges the
    #: recipe's defaults itself, so only these are handed to it.
    overrides: dict[str, Any]
    defaults: dict[str, Any]
    extra_args: list[str]
    #: The API port the caller named — a pin, kept as given — else ``None``.
    port_pin: int | None
    #: The recipe's port, a preference a backend may move; ``None`` for none.
    port_preference: int | None
    #: The ports the engine binds itself; never handed out as the API port.
    rendezvous_port: int | None
    rpc_port: int | None
    deployment_id: str
    name: str
    serves: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The intent as stored on a run: what it is, credentials redacted.

        Not its identity (``deployment_id``, ``name``), which the record holds
        already, nor what was true when it was resolved (``model_present``,
        ``warnings``): two runs of the same thing have the same intent.
        """
        recipe = {k: v for k, v in self.recipe.items() if k not in RECIPE_ANNOTATIONS}
        return _redacted(
            {
                "recipe_id": self.recipe_id,
                "recipe": recipe,
                "engine": self.engine_name,
                "variant": self.variant,
                "image_ref": self.image_ref,
                "model": self.model,
                "model_requested": self.model_requested,
                "nodes": list(self.nodes),
                "topology": [asdict(node) for node in self.topology.nodes],
                "mods": list(self.mods),
                "overrides": dict(self.overrides),
                "defaults": dict(self.defaults),
                "extra_args": list(self.extra_args),
                "port_pin": self.port_pin,
                "port_preference": self.port_preference,
                "rendezvous_port": self.rendezvous_port,
                "rpc_port": self.rpc_port,
                "serves": self.serves,
            }
        )

    def intent_hash(self) -> str:
        """SHA-256 over :meth:`to_dict`'s canonical JSON.

        Canonical — sorted keys, no whitespace, non-JSON values as their string
        — so the same intent hashes the same in any process on any machine.
        Over the redacted form, so a rotated token is not a different run.
        """
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def reserved_ports(self) -> list[tuple[int | None, str]]:
        return [(self.rendezvous_port, "rendezvous"), (self.rpc_port, "RPC")]

    def render(self, port: int, model_file: str) -> list[Any]:
        """Each rank's launch, for the API port and model path a backend chose.

        The capacity check runs on the rendered command because that is what
        the engine is handed: a v1 template spells its parallelism out itself.
        """
        overrides = {**self.overrides, "port": port}
        try:
            ranks = [
                self.engine.render(
                    self.recipe,
                    model=self.model_requested,
                    params=overrides,
                    extra_args=self.extra_args,
                    topology=self.topology,
                    node_rank=rank,
                    model_file=model_file,
                )
                for rank in range(self.topology.size)
            ]
        except EngineError as exc:
            raise NativeRuntimeError(str(exc)) from exc
        if self.engine.parallelism_in_command:
            _check_capacity(self.recipe_id, ranks[0].command, self.topology.size)
        return ranks

    def params_for(self, port: int) -> dict[str, Any]:
        """The params the run is recorded with: defaults, overrides, the port."""
        merged = {**self.defaults, **self.overrides, "port": port}
        merged.setdefault("host", "0.0.0.0")
        return merged


def resolve_intent(
    recipe_id: str,
    *,
    registry: Any,
    engine: str | None = None,
    variant: str | None = None,
    model: str | None = None,
    params: dict[str, Any] | None = None,
    extra_args: list[str] | None = None,
    nodes: list[str] | None = None,
    solo: bool = True,
    name: str = "",
    deployment_id: str | None = None,
    allow_missing_model: bool = False,
) -> RunIntent:
    """Resolve what a run is, refusing what no backend could run.

    ``registry`` is the engine registry, passed in rather than fetched so the
    caller decides which one (``native_runtime.plan`` hands its own, which is
    what tests patch).
    """
    recipe = tools.recipes.get_recipe(recipe_id)
    if recipe is None:
        raise NativeRuntimeError(f"recipe '{recipe_id}' not found")

    warnings: list[str] = []

    # An empty node list is not "no nodes": it is this machine. The topology
    # is total, so every size below takes the same code path.
    node_list = [] if solo else [str(n) for n in (nodes or [])]
    # The recipe's own constraints are checked against the requested count
    # first, so "this recipe is solo_only" is reported before anything about
    # the registry: it is the more specific answer.
    _check_constraints(recipe_id, recipe, max(1, len(node_list)))
    topology = _resolve_topology(node_list, warnings)

    engine_obj, engine_name, resolved_variant = _select_engine(
        registry, recipe, engine, variant
    )

    supported, reason = engine_obj.supports(recipe)
    if not supported:
        raise NativeRuntimeError(
            f"engine '{engine_name}/{resolved_variant}' cannot run recipe "
            f"'{recipe_id}': {reason}"
        )

    engine_obj, engine_name, resolved_variant, image_ref = _resolve_image(
        registry,
        engine_obj,
        engine_name,
        resolved_variant,
        recipe,
        explicit_engine=bool(engine),
        warnings=warnings,
    )

    # Checked after the image is resolved, because a legacy container tag can
    # map to an older image than the engine's default variant would use — and
    # the capabilities travel with the image, so the size claim does too.
    size_ok, size_reason = engine_obj.supports_size(topology.size)
    if not size_ok:
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' cannot be planned on {topology.size} "
            f"node(s): {size_reason}"
        )

    version_ok, version_reason = engine_obj.version_supported()
    if not version_ok:
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' cannot be planned: {version_reason}"
        )
    if version_reason:
        warnings.append(version_reason)

    resolved_model, model_present = _resolve_model(
        recipe, model, allow_missing_model, warnings
    )

    mods = engine_obj.block_mods(recipe)
    if mods and not engine_obj.supports_mods():
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' needs mods ({', '.join(mods)}) but engine "
            f"'{engine_name}' does not support them"
        )

    overrides = {k: v for k, v in (params or {}).items() if v is not None}
    defaults = recipe.get("defaults") or {}
    rendezvous_port = engine_obj.rendezvous_port()
    # Only above one node: at one node there is no worker to run an
    # rpc-server, so nothing binds it and it is not a port this deploy holds.
    rpc_port = engine_obj.rpc_port() if topology.size > 1 else None

    port_pin: int | None = None
    if overrides.get("port") not in (None, "", 0):
        # The caller named it: a pin. Kept exactly as asked, and a port that
        # turns out to be taken is the pre-flight's to block, not a backend's
        # to move — somebody chose that number for a client that will dial it.
        port_pin = int(overrides["port"])
        for reserved, role in ((rendezvous_port, "rendezvous"), (rpc_port, "RPC")):
            if reserved and port_pin == int(reserved):
                raise NativeRuntimeError(
                    f"port {port_pin} is the {role} port of engine "
                    f"'{engine_name}/{resolved_variant}'; the launch binds it "
                    "itself, so pick another API port"
                )
    preferred = defaults.get("port")

    return RunIntent(
        recipe_id=recipe_id,
        recipe=recipe,
        engine=engine_obj,
        engine_name=engine_name,
        variant=resolved_variant,
        image_ref=image_ref,
        model=resolved_model,
        model_requested=model,
        model_present=model_present,
        topology=topology,
        nodes=node_list,
        mods=mods,
        overrides=overrides,
        defaults=defaults,
        extra_args=list(extra_args or []),
        port_pin=port_pin,
        port_preference=int(preferred) if preferred not in (None, "", 0) else None,
        rendezvous_port=rendezvous_port,
        rpc_port=rpc_port,
        deployment_id=deployment_id or uuid.uuid4().hex[:12],
        name=name or str(recipe.get("name") or recipe_id),
        serves=tools.recipe_schema.serves_of(recipe),
        warnings=warnings,
    )


def _select_engine(
    registry: Any,
    recipe: dict[str, Any],
    engine: str | None,
    variant: str | None,
) -> tuple[Engine, str, str]:
    override = engine
    if override and variant:
        override = f"{override}/{variant}"
    elif variant and not override:
        override = f"{recipe.get('engine') or config.default_engine}/{variant}"
    try:
        engine_name, resolved_variant = registry.select(
            request_override=override,
            recipe_engine=recipe.get("engine"),
            default_engine=config.default_engine,
        )
        return (
            registry.engine(engine_name, resolved_variant),
            engine_name,
            resolved_variant,
        )
    except (EngineNotFound, EngineError) as exc:
        raise NativeRuntimeError(str(exc)) from exc


def _resolve_image(
    registry: Any,
    engine_obj: Engine,
    engine_name: str,
    variant: str,
    recipe: dict[str, Any],
    explicit_engine: bool,
    warnings: list[str],
) -> tuple[Engine, str, str, str]:
    """Resolve the image ref, mapping a v1 ``container:`` tag when possible.

    A v1 recipe names an upstream image tag (``vllm-node``). The engine
    registry claims those via ``legacy_tags``, so the tag picks the exact spec
    (and therefore the digest) instead of the engine's default variant. An
    explicit engine override in the request always wins.
    """
    tag = str(recipe.get("container") or "").strip()
    if tag and not explicit_engine:
        try:
            spec = registry.resolve_legacy_tag(tag)
        except EngineNotFound:
            warnings.append(
                f"recipe container tag '{tag}' is not claimed by any engine; "
                f"using the {engine_name}/{variant} default image"
            )
        else:
            if spec.engine != engine_name:
                warnings.append(
                    f"recipe container tag '{tag}' belongs to engine "
                    f"'{spec.engine}' but '{engine_name}' was selected; "
                    "using the selected engine's default image"
                )
            else:
                engine_obj = registry.engine(spec.engine, spec.variant)
                return engine_obj, spec.engine, spec.variant, spec.image_ref
    image_ref = engine_obj.default_image()
    if not image_ref:
        raise NativeRuntimeError(
            f"engine '{engine_name}/{variant}' declares no image; "
            "refresh the engine index or pin an image in settings"
        )
    return engine_obj, engine_name, variant, image_ref


def _resolve_model(
    recipe: dict[str, Any],
    model: str | None,
    allow_missing_model: bool,
    warnings: list[str],
) -> tuple[str, bool]:
    """Resolve the model and say whether it is on this machine.

    The presence check runs even when ``allow_missing_model`` is set, because
    the two questions are different: *may* this proceed without the model, and
    *is* the model here. A plan is a dry run and always permits a missing
    model, which meant the preview could not tell the operator what the deploy
    was about to refuse — they found out from a 400 after pressing Deploy.
    The answer now travels on the plan as ``model_present``.
    """
    resolved = str(model or recipe.get("model") or "").strip()
    if not resolved or resolved == "unknown":
        # v1 recipes embed the model in the command template; nothing to check.
        return "", True
    try:
        entry = tools.models.get_model(resolved)
    except Exception as exc:  # pragma: no cover - catalogue is best effort
        warnings.append(f"model catalogue unavailable: {exc}")
        # Unknown, not absent. Reported as present so an unreachable catalogue
        # cannot invent a missing model and offer to download one that is
        # already here.
        return resolved, True
    if entry is None:
        if not allow_missing_model:
            raise MissingModelError(resolved)
        warnings.append(
            f"model '{resolved}' is not in the local catalogue and would have "
            "to be downloaded before this can run"
        )
        return resolved, False
    return resolved, True


def _check_constraints(recipe_id: str, recipe: dict[str, Any], nodes: int) -> None:
    """Enforce the recipe's topology constraints against the real node count.

    The recipe parser has produced ``solo_only`` / ``cluster_only`` /
    ``min_nodes`` all along and nothing read them; a topology that is total is
    what makes them checkable in one place.
    """
    if recipe.get("solo_only") and nodes > 1:
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' is marked solo_only, so it cannot be "
            f"deployed across {nodes} nodes; deploy it on one node"
        )
    if recipe.get("cluster_only") and nodes < 2:
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' is marked cluster_only, so it needs at "
            f"least 2 nodes; {nodes} was requested"
        )
    minimum = recipe.get("min_nodes")
    if isinstance(minimum, int) and nodes < minimum:
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' needs at least {minimum} nodes; "
            f"{nodes} was requested"
        )


def _check_capacity(recipe_id: str, command: str, nodes: int) -> None:
    """Refuse a launch that asks for more GPUs than the nodes hold.

    One GPU per node is the hardware, so tensor parallelism spans nodes and a
    tp of 2 simply needs a second Spark. This replaces the old silent rewrite
    of solo deployments to ``tensor_parallel=1``: an operator who asked for
    two-way parallelism on one node now hears why it cannot happen instead of
    quietly getting something else.
    """
    parallelism = parse_parallelism(command)
    needed = parallelism["tp"] * parallelism["pp"] * parallelism["dp"]
    shape = f"tp={parallelism['tp']} pp={parallelism['pp']} dp={parallelism['dp']}"
    ok, message = validate_cluster_capacity(
        parallelism, ClusterCapacity.for_nodes(nodes)
    )
    if not ok:
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' does not fit {nodes} node(s): {message} "
            f"({shape}). "
            "This hardware has one GPU per node, so either lower the "
            f"parallelism or deploy across {needed} nodes"
        )
    if needed < nodes:
        # Upstream trimmed the extra peers silently (launch-cluster.sh line
        # 1267). Refusing is the honest version, and vLLM agrees: above one
        # node it requires --nnodes to divide the world size exactly and
        # raises "must evenly divide the total world size" otherwise
        # (vllm/engine/arg_utils.py, since 0.11.1). So a trimmed launch does
        # not hang — it fails on every rank with an argument error, N
        # containers after the point where we could have said this once.
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' asks for {nodes} nodes but its parallelism "
            f"only occupies {needed} of them ({shape}). One GPU per node means "
            f"the world size is the node count, and vLLM refuses a --nnodes "
            f"that does not divide the world size exactly, so this would fail "
            f"on every rank rather than serve on a subset. Deploy on "
            f"{needed} node(s), or raise the parallelism until tp*pp*dp is "
            f"{nodes}"
        )


def _registry_by_address() -> dict[str, Any]:
    """Every registered node, keyed by the address a deploy would name.

    Raising rather than degrading is deliberate: a multi-node plan reads the
    registry for the interface names it pins, and pinning is find-or-fail. A
    registry we cannot read is not "no interfaces", it is "we do not know".
    """
    try:
        nodes = list(tools.node_registry.list_nodes())
    except Exception as exc:
        raise NativeRuntimeError(
            f"the node registry could not be read, so a multi-node "
            f"deployment cannot resolve its nodes: {exc}"
        ) from exc
    return {node.address: node for node in nodes if node.address}


def _resolve_topology(node_list: list[str], warnings: list[str]) -> Topology:
    """The requested addresses as a topology carrying real interface names.

    An empty list is this machine — the size-one case, which never consults
    the registry at all, so nothing about a solo deployment depends on what
    the registry holds.

    Above one node the registry is the authority, and it is the *only*
    authority: it is where an operator records which interface on that
    particular machine carries the fabric, and interface pinning is
    find-or-fail. An address we have no record for would be launched with no
    pinning at all, so it is refused rather than started blind.
    """
    if not node_list:
        return Topology(nodes=[])

    seen: set[str] = set()
    for address in node_list:
        if address in seen:
            raise NativeRuntimeError(
                f"node '{address}' is listed twice; each rank runs on its own "
                "machine, and one GPU per node means a machine cannot hold two"
            )
        seen.add(address)

    records = _registry_by_address()
    if len(node_list) > len(records):
        known = ", ".join(sorted(records)) or "none"
        raise NativeRuntimeError(
            f"{len(node_list)} nodes were requested but the registry holds "
            f"{len(records)} ({known}). Enroll the missing machines on the "
            "Cluster page (POST /api/nodes) before deploying across them"
        )
    unknown = [address for address in node_list if address not in records]
    if unknown:
        known = ", ".join(sorted(records)) or "none"
        raise NativeRuntimeError(
            f"node(s) {', '.join(unknown)} are not in the node registry "
            f"(it holds {known}). A peer is deployed to by its registry "
            "record, which is where its fabric interface names live; NCCL "
            "pinning is find-or-fail, so an unregistered address would be "
            "launched with no pinning at all"
        )

    nodes: list[NodeInfo] = []
    unpinned: list[str] = []
    by_fabric: dict[str, list[str]] = {}
    for address in node_list:
        record = records[address]
        if not record.ethernet_interface and not record.infiniband_interfaces:
            unpinned.append(record.label)
        if record.fabric_mode:
            by_fabric.setdefault(record.fabric_mode, []).append(record.label)
        nodes.append(
            NodeInfo(
                host=address,
                ip=address,
                # Only a verified fabric apply writes these, so an empty tuple
                # means "this control plane has not configured a fabric
                # address on that machine" — never a guess about cabling. An
                # engine that can use a second wire reads them; nothing else
                # about the launch changes.
                fabric_addresses=tuple(record.fabric_addresses),
                eth_if=record.ethernet_interface,
                # NCCL_IB_HCA takes a comma-separated selector list, which is
                # the order discovery reported the fabric ports in. It holds
                # both RoCE twins of every cabled port; naming one halves the
                # bandwidth without failing.
                ib_if=",".join(record.infiniband_interfaces),
                mesh=record.fabric_mode == FABRIC_MESH,
            )
        )
    if len(by_fabric) > 1:
        # A mesh is a ring every member takes part in — port 0 of one Spark
        # into port 1 of the next, all four ports up. One node cabled that way
        # and another on a single cable is not a fabric, and it decides three
        # NCCL settings that either apply to the whole collective or to none
        # of it.
        described = "; ".join(
            f"{mode}: {', '.join(sorted(labels))}"
            for mode, labels in sorted(by_fabric.items())
        )
        raise NativeRuntimeError(
            f"the nodes disagree about how the fabric is cabled ({described}). "
            "A switchless mesh is a ring every node is part of, and it needs "
            "NCCL settings a single-cable fabric must not get, so the "
            "collective cannot be configured for both. Re-run discovery on "
            "the nodes whose cabling changed, or correct fabric_mode on their "
            "registry records (PATCH /api/nodes/{id})"
        )
    if FABRIC_MESH in by_fabric and len(node_list) != MESH_RING_NODES:
        raise NativeRuntimeError(
            f"{', '.join(sorted(by_fabric[FABRIC_MESH]))} report the switchless "
            f"ring, which NVIDIA documents at exactly {MESH_RING_NODES} nodes "
            f"and not at {len(node_list)}. Its own NCCL launcher refuses any "
            "other count outright, and a four-node ring has no published "
            "cabling, no published NCCL configuration and no reference "
            "bandwidth. Deploy the ring on three nodes, or put the cluster "
            "behind a QSFP switch and re-run discovery so the nodes report a "
            "single cable"
        )
    # This is about a collective that crosses machines, and naming one node
    # explicitly is not that. `nodes: ["192.168.29.60"]` is how the UI pins a
    # solo run to a particular Spark, and on a node with no interface names
    # recorded it arrived here saying NCCL would choose a link itself — which
    # one rank never asks it to do. The size is the topology's, so it is the
    # same test `Topology.is_solo` makes.
    if len(nodes) > 1 and unpinned:
        warnings.append(
            f"no interface names are recorded for {', '.join(unpinned)}, "
            "so NCCL will choose a link itself — usually the management "
            "one, which is a performance bug rather than a failure. "
            "Record them on the node's registry entry "
            "(PATCH /api/nodes/{id})"
        )
    return Topology(nodes=nodes)
