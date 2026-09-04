"""Native solo deployment runtime — Docker driven from Python.

This is the phase-1 replacement for forking upstream's ``run-recipe.sh``. It
reproduces the upstream lifecycle described in the native-runtime plan §1.4:

1. :func:`plan` resolves everything up front — engine, image, model, mods,
   port, container profile and the rendered per-rank launch script — and
   returns a serialisable :class:`DeployPlan`. Nothing is started, so the same
   call backs the UI's "Preview" button and ``POST /api/deployments/plan``.
2. :func:`start` loops over the plan's ranks. Each rank runs an *idle*
   container (``sleep infinity``) on its own node, has the recipe's mods
   applied with ``docker exec``, gets the rendered script copied to
   ``/workspace/exec-script.sh`` and exec'd detached with output redirected to
   PID 1's stdout so ``docker logs`` carries the serve output; then rank zero's
   readiness endpoint is polled.

The gang semantics are the ones §3.3 of ``docs/cluster-agent-plan.md`` takes
from every system surveyed:

* **Ordered.** Every container is created first and only then launched —
  workers first, rank zero last, which is upstream's order. Teardown is the
  reverse, rank zero first, so a worker is not left blocking on a store whose
  server has gone. That block is bounded by PyTorch's ``init_process_group``
  timeout, which defaults to ten minutes for NCCL and thirty for gloo and
  which vLLM leaves alone unless ``--distributed-timeout-seconds`` is passed.
  It is *PyTorch's* timeout: NCCL itself has no collective timeout and no
  environment variable for one.
* **All-or-nothing.** Any rank failing fails the deployment. There is no
  partial state and no per-rank restart, because the model is sharded across
  exactly those ranks. Docker's restart policy is ``no`` so a rebooting node
  cannot resurrect a rank into a torn-down deployment.
* **Generational.** A container is named ``spark-pulse-<deployment>-r<rank>-
  g<generation>``, so a container from an earlier attempt has a different name
  and is unambiguously reapable. Every rank of a generation is confirmed gone
  before any rank of the next is created.
* **Released on evidence.** A rank on a node we cannot reach cannot be torn
  down; it is recorded as an outstanding orphan and that node's ports stay
  held until something confirms the container is gone.

At one node every loop has length one, so the observable behaviour is the one
that ran before ranks existed. That is the property that makes this safe.

Everything switchable goes through ``spark_pulse.tools`` so simulation mode
swaps the container service; ``spark_pulse.engines`` is imported directly
because rendering is pure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.engines import (
    Engine,
    EngineError,
    EngineNotFound,
    NodeInfo,
    Topology,
    get_registry,
)
from spark_pulse.tools.discovery import FABRIC_MESH, MESH_RING_NODES
from spark_pulse.tools.docker import ContainerMetadata, PullCancelled
from spark_pulse.tools.events import DeploymentEvent, EventType
from spark_pulse.tools.labels import (
    DEPLOYMENT_LABEL,
    GENERATION_LABEL,
    RANK_LABEL,
    WORLD_SIZE_LABEL,
)
from spark_pulse.tools.labels import identity_labels as identity_labels

# Capacity validation is pure arithmetic with no side effect to simulate, so
# it is imported directly, like ``spark_pulse.engines``.
from spark_pulse.tools.parallelism import (
    ClusterCapacity,
    parse_parallelism,
    validate_cluster_capacity,
)

logger = logging.getLogger(__name__)

RUNTIME_NAME = "native"
SCRIPT_PATH = "/workspace/exec-script.sh"
MODS_DIR = "/workspace/mods"
CONTAINER_PREFIX = "spark-pulse-"
CONTAINER_HOME = "/root"
HF_CACHE_IN_CONTAINER = "/root/.cache/huggingface"

#: How long to wait for evidence that a container is really gone, and how
#: often to look. Removal is fast; the wait exists so the next generation
#: never races a rank that is still holding the GPU.
CONFIRM_GONE_TIMEOUT = 30.0
CONFIRM_GONE_INTERVAL = 0.5

#: Attached to every plan above one node, and to the record it becomes.
#:
#: Multi-node is implemented and exercised end to end in simulation — every
#: rank rendered, started worker-first, torn down head-first and accounted for.
#: It has never been run on two machines, because there is only one DGX Spark.
#: The full list of what a second machine would prove is in
#: ``docs/cluster-agent-plan.md`` section 7 and in the UI banner; this is the
#: one line that travels with the plan itself.
MULTI_NODE_UNPROVEN = (
    "multi-node has never been run on hardware: only one DGX Spark exists, so "
    "the rendering, ordering and bookkeeping below are exercised in simulation "
    "and nothing about the rendezvous forming across machines, NCCL transport "
    "over the real fabric or interface pinning against real per-role names has "
    "been observed"
)


class NativeRuntimeError(RuntimeError):
    """A native deployment could not be planned or started."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Event publishing ─────────────────────────────────────────────────────────
#
# Deploys run on a request thread while the shared EventBroadcaster in
# ``sse.py`` is asyncio-based. Same arrangement as ``tools.models``: the SSE
# generator registers its loop, and with no listener there is simply nothing to
# deliver.

_loop: asyncio.AbstractEventLoop | None = None


def register_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Record the loop SSE consumers run on (called from ``sse.py``)."""
    global _loop
    _loop = loop


def publish_event(
    event_type: EventType,
    deployment_id: str,
    message: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a deployment event on the shared broadcaster from any thread."""
    from spark_pulse.sse import _get_event_broadcaster

    event = DeploymentEvent(
        event_type=event_type,
        resource=deployment_id,
        resource_type="deployment",
        message=message or event_type.value,
        metadata=metadata or {},
    )
    try:
        broadcaster = _get_event_broadcaster()
    except Exception:  # pragma: no cover - defensive
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        running.create_task(broadcaster.emit(event))
        return
    if _loop is not None and _loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcaster.emit(event), _loop)


# ── Plan model ───────────────────────────────────────────────────────────────


@dataclass
class ContainerSpec:
    """Everything needed to start the deployment's container."""

    image: str
    name: str
    command: str = "sleep infinity"
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    mounts: dict[str, str] = field(default_factory=dict)
    privileged: bool = True
    ipc_host: bool = True
    network_host: bool = True
    shm_size_gb: float = 64
    devices: list[str] = field(default_factory=list)
    cap_add: list[str] = field(default_factory=list)
    ulimits: dict[str, str] = field(default_factory=dict)
    memory_limit_gb: float | None = None
    pids_limit: int = 4096
    nofile_limit: int = 1048576
    port_mappings: list[str] = field(default_factory=list)
    entrypoint_clear: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RankPlan:
    """One rank of the gang: which node, which container, which script.

    ``node`` is the address the rank runs on. Empty means this machine, which
    is what a size-one deployment carries — the same sentinel the record has
    always used, so nothing that reads it has to change.
    """

    rank: int
    node: str
    host: str
    container: ContainerSpec
    command: str
    script: str
    is_head: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["container"] = self.container.to_dict()
        return data


@dataclass
class DeployPlan:
    """The resolved, serialisable result of planning a deployment."""

    deployment_id: str
    recipe_id: str
    recipe_name: str
    name: str
    engine: str
    variant: str
    image_ref: str
    model: str
    solo: bool
    nodes: list[str]
    node_count: int
    port: int
    rendezvous_port: int | None
    readiness_path: str
    readiness_url: str
    metrics_path: str | None
    mods: list[str]
    params: dict[str, Any]
    extra_args: list[str]
    launch_command: str
    ranks: list[dict[str, Any]]
    container: ContainerSpec
    cache_mounts: list[str]
    #: Every rank, in rank order. ``rank_plans[0]`` is the head, and its
    #: container is the same object as :attr:`container` — the scalar is a
    #: derived alias kept for readers that predate ranks.
    rank_plans: list[RankPlan] = field(default_factory=list)
    #: Monotonic attempt counter for this deployment id. Carried in every
    #: container name and label so a leftover rank is reapable by name.
    generation: int = 1
    image_present: bool = True
    image_size_bytes: int | None = None
    workdir: str = ""
    warnings: list[str] = field(default_factory=list)
    runtime: str = RUNTIME_NAME
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["container"] = self.container.to_dict()
        data["rank_plans"] = [r.to_dict() for r in self.rank_plans]
        return data

    @property
    def head(self) -> RankPlan:
        """Rank zero — the rank that serves the API."""
        return self.rank_plans[0]

    def start_order(self) -> list[RankPlan]:
        """Workers first, rank zero last: upstream's proven order."""
        return list(reversed(self.rank_plans))

    def teardown_order(self) -> list[RankPlan]:
        """Rank zero first, so no worker sits blocked on a store that is gone."""
        return list(self.rank_plans)


# ── Helpers ──────────────────────────────────────────────────────────────────


def container_name_for(deployment_id: str) -> str:
    """The rank-less container name records used before ranks existed.

    Still the fallback for a record that carries no name of its own, so a
    deployment started by an earlier build stays stoppable, readable and
    reportable. Nothing new is created under this name.
    """
    return f"{CONTAINER_PREFIX}{deployment_id}"


def rank_container_name(deployment_id: str, rank: int, generation: int) -> str:
    """``spark-pulse-<deployment>-r<rank>-g<generation>``.

    Deterministic, so Docker's atomic name reservation is the exactly-once
    primitive. The generation is what makes a container from an abandoned
    attempt a *different* name: leftovers are reaped by evidence rather than
    adopted by accident.
    """
    return f"{CONTAINER_PREFIX}{deployment_id}-r{rank}-g{generation}"


def _next_generation(deployment_id: str) -> int:
    """One past whatever this deployment last ran at; 1 when it is new."""
    record = get_deployment(deployment_id)
    if record is None:
        return 1
    current = record.get("generation")
    return (current if isinstance(current, int) and current > 0 else 0) + 1


def rank_entries(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The record's per-rank list, synthesised for records that predate it.

    A pre-rank record carries only the scalar ``container_name``; it becomes a
    one-element list naming rank zero on this machine, which is exactly what
    it always was.
    """
    ranks = record.get("ranks")
    if isinstance(ranks, list) and ranks:
        return [dict(entry) for entry in ranks]
    name = record.get("container_name") or container_name_for(
        str(record.get("id") or "")
    )
    return [
        {
            "rank": 0,
            "node": "",
            "host": "",
            "container_name": name,
            "is_head": True,
        }
    ]


def _docker_service() -> Any:
    """The container service for this process (real or mock)."""
    return tools.docker._get_service()


def rank_services(docker: Any | None = None) -> Callable[[str], Any]:
    """Resolve the container service bound to a rank's node address.

    ``docker`` pins one service for every rank, which is what a caller that
    already holds a service (and every test) wants. Otherwise the empty
    address — the record's long-standing sentinel for this machine — goes
    straight to the process's own container service, and a real address goes
    through the node-bound resolver, which decides local versus SSH once.
    """
    if docker is not None:
        return lambda _address: docker

    resolver: Any = None

    def _resolve(address: str) -> Any:
        nonlocal resolver
        if not address:
            return _docker_service()
        if resolver is None:
            resolver = tools.node_service.NodeServices()
        return resolver.for_address(address)

    return _resolve


def _rank_is_here(entry: dict[str, Any]) -> bool:
    """Whether this rank's container lives on the machine we can enumerate."""
    address = str(entry.get("node") or "")
    if not address:
        return True
    try:
        return bool(tools.node_service.is_local_address(address))
    except Exception:  # pragma: no cover - discovery is best effort
        return False


def _inspect_image(image_ref: str, warnings: list[str]) -> tuple[bool, int | None]:
    """Report whether ``image_ref`` is on this host, and how big it is.

    A missing image is not a planning failure — it just means the deploy has a
    pull in front of it, which is exactly what the caller wants to be told
    about up front. An unreachable Docker daemon is reported the same way: the
    presence is unknown, so assume a pull.
    """
    try:
        docker = _docker_service()
        if docker.image_exists(image_ref):
            info = docker.image_info(image_ref) or {}
            return True, int(info.get("size_bytes") or 0) or None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("could not inspect image %s: %s", image_ref, exc)
        warnings.append(
            f"could not check whether image '{image_ref}' is present: {exc}"
        )
        return False, None
    warnings.append(
        f"image '{image_ref}' is not on this host; it will be pulled before "
        "the container starts, which can take tens of minutes"
    )
    return False, None


def _expand(path: str) -> str:
    return str(Path(os.path.expanduser(str(path))))


def _container_path(host_path: str) -> str:
    """Where a host cache dir lands inside the container.

    Upstream mounts ``~/.cache/vllm`` at ``/root/.cache/vllm``: the container
    runs as root, so the user's home prefix is rewritten.
    """
    home = str(Path.home())
    if host_path == home:
        return CONTAINER_HOME
    if host_path.startswith(home + os.sep):
        return CONTAINER_HOME + host_path[len(home) :]
    return host_path


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def allocate_port(taken: set[int] | None = None) -> int:
    """First free port in the configured range, skipping ``taken``."""
    taken = taken or set()
    start, end = config.default_port_range_start, config.default_port_range_end
    for port in range(start, end + 1):
        if port in taken:
            continue
        if _port_free(port):
            return port
    raise NativeRuntimeError(
        f"no free port in the configured range {start}-{end}; "
        "widen default_port_range_* or stop an existing deployment"
    )


def _ports_in_use() -> set[int]:
    """Ports live deployments hold — API *and* rendezvous.

    A launch binds its rendezvous port exactly as surely as its API port, so
    handing the same number out twice would break a deployment that never
    mentioned it.

    A stopped deployment with outstanding orphans still holds its ports. Its
    ranks on unreachable nodes were never confirmed gone, and every orphan bug
    in this class comes from releasing a resource on inference — "we asked it
    to stop" — rather than on evidence that it did.
    """
    ports: set[int] = set()
    for record in _load_records():
        if record.get("status") in ("stopped", "error") and not record.get("orphans"):
            continue
        for key in ("port", "rendezvous_port"):
            value = record.get(key)
            if isinstance(value, int):
                ports.add(value)
    return ports


# ── Persistence (shared deployments.json) ────────────────────────────────────


def _load_records() -> list[dict[str, Any]]:
    """Load the deployment records.

    ``deployment_records`` owns the file, including its refusal to read an
    unreadable state file as an empty one: ``StateFileError`` propagates to the
    caller rather than degrading into ``[]``. Records made by the removed
    upstream runner live in the same file and are simply not ours.
    """
    return tools.deployment_records.load()


def _save_records(records: list[dict[str, Any]]) -> None:
    """Persist the deployment records.

    Delegates to ``deployment_records.save``, which writes through
    ``atomic_json.write_json_atomic`` — temp file, fsync, replace, dir fsync.
    """
    tools.deployment_records.save(records)


def _update_record(deployment_id: str, **fields: Any) -> dict[str, Any] | None:
    records = _load_records()
    for record in records:
        if record.get("id") == deployment_id:
            record.update(fields)
            _save_records(records)
            return record
    return None


def get_deployment(deployment_id: str) -> dict[str, Any] | None:
    """The persisted record for ``deployment_id``, native or not."""
    return next((r for r in _load_records() if r.get("id") == deployment_id), None)


def is_native(record: dict[str, Any] | None) -> bool:
    return bool(record) and record.get("runtime") == RUNTIME_NAME  # type: ignore[union-attr]


# ── Planning ─────────────────────────────────────────────────────────────────


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
) -> str:
    resolved = str(model or recipe.get("model") or "").strip()
    if not resolved or resolved == "unknown":
        # v1 recipes embed the model in the command template; nothing to check.
        return ""
    if allow_missing_model:
        return resolved
    try:
        entry = tools.models.get_model(resolved)
    except Exception as exc:  # pragma: no cover - catalogue is best effort
        warnings.append(f"model catalogue unavailable: {exc}")
        return resolved
    if entry is None:
        raise NativeRuntimeError(
            f"model '{resolved}' is not in the local catalogue; "
            "download it first or deploy with allow_missing_model"
        )
    return resolved


def _container_profile(engine_obj: Engine) -> dict[str, Any]:
    """Engine profile with the user's ``docker:`` block layered on top."""
    profile = dict(engine_obj.container_profile())
    overrides = config.docker_overrides
    for key in (
        "privileged",
        "ipc_host",
        "network_host",
        "shm_size_gb",
        "devices",
        "cap_add",
        "ulimits",
        "keepalive",
    ):
        if key in overrides and overrides[key] is not None:
            profile[key] = overrides[key]
    return profile


def _build_env(
    engine_obj: Engine,
    recipe: dict[str, Any],
    topology: Topology,
    node_rank: int = 0,
) -> dict[str, str]:
    node = topology.nodes[node_rank]
    env = dict(
        engine_obj.base_env(
            node_ip=node.ip,
            eth_if=node.eth_if,
            ib_if=node.ib_if,
            node_count=topology.size,
            mesh=node.mesh,
        )
    )
    # Per-engine env, so deploying a v2 recipe on its non-default engine does
    # not inherit the other engine's variables.
    env.update(engine_obj.block_env(recipe))
    env.setdefault("HF_HOME", HF_CACHE_IN_CONTAINER)
    token = config.hf_token
    if token:
        env["HF_TOKEN"] = token
    return env


def _build_mounts(engine_obj: Engine) -> tuple[dict[str, str], list[str]]:
    """Host->container bind mounts for the engine's caches plus ``HF_HOME``."""
    mounts: dict[str, str] = {}
    declared = list(engine_obj.cache_mounts())
    for raw in declared:
        host = _expand(raw)
        mounts[host] = _container_path(host)
    try:
        hf_home = str(tools.models.hf_home())
    except Exception:  # pragma: no cover - defensive
        hf_home = _expand("~/.cache/huggingface")
    # HF_HOME wins over any engine-declared cache that targets the same path;
    # docker refuses two binds on one container destination.
    for host, target in list(mounts.items()):
        if target == HF_CACHE_IN_CONTAINER:
            del mounts[host]
    mounts[hf_home] = HF_CACHE_IN_CONTAINER
    return mounts, declared


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
    warnings.append(MULTI_NODE_UNPROVEN)
    if unpinned:
        warnings.append(
            f"no interface names are recorded for {', '.join(unpinned)}, so "
            "NCCL will choose a link itself — usually the management one, "
            "which is a performance bug rather than a failure. Record them on "
            "the node's registry entry (PATCH /api/nodes/{id})"
        )
    return Topology(nodes=nodes)


def plan(
    recipe_id: str,
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
) -> DeployPlan:
    """Resolve a deployment without starting anything.

    Raises :class:`NativeRuntimeError` with an explained reason whenever the
    deployment cannot run — that is the whole point of the dry run.
    """
    registry = get_registry()
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

    resolved_model = _resolve_model(recipe, model, allow_missing_model, warnings)

    mods = engine_obj.block_mods(recipe)
    if mods and not engine_obj.supports_mods():
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' needs mods ({', '.join(mods)}) but engine "
            f"'{engine_name}' does not support them"
        )

    # Only the caller's own overrides are handed to the engine: it merges the
    # recipe's defaults itself.
    overrides = {k: v for k, v in (params or {}).items() if v is not None}
    defaults = recipe.get("defaults") or {}
    rendezvous_port = engine_obj.rendezvous_port()
    port = overrides.get("port") or defaults.get("port")
    if port in (None, "", 0):
        taken = _ports_in_use()
        if rendezvous_port:
            taken.add(rendezvous_port)
        port = allocate_port(taken)
        overrides["port"] = port
    port = int(port)
    if rendezvous_port and port == rendezvous_port:
        raise NativeRuntimeError(
            f"port {port} is the rendezvous port of engine "
            f"'{engine_name}/{resolved_variant}'; the launch binds it itself, "
            "so pick another API port"
        )
    if "port" in overrides:
        overrides["port"] = port
    merged = {**defaults, **overrides, "port": port}
    merged.setdefault("host", "0.0.0.0")

    try:
        ranks = [
            engine_obj.render(
                recipe,
                model=model,
                params=overrides,
                extra_args=extra_args or [],
                topology=topology,
                node_rank=rank,
            )
            for rank in range(topology.size)
        ]
    except EngineError as exc:
        raise NativeRuntimeError(str(exc)) from exc

    _check_capacity(recipe_id, ranks[0].command, topology.size)

    dep_id = deployment_id or uuid.uuid4().hex[:12]
    generation = _next_generation(dep_id)
    profile = _container_profile(engine_obj)
    mounts, cache_mounts = _build_mounts(engine_obj)
    network_host = bool(profile.get("network_host", False))
    # In solo the API port is published only when the container is not on the
    # host network; on host networking the engine already binds it directly.
    port_mappings = [] if network_host else [f"{port}:{port}"]
    created_at = _now()
    mode = "solo" if topology.is_solo else "cluster"
    shm_size = float(profile.get("shm_size_gb") or config.docker_shm_size_gb)
    privileged = bool(profile.get("privileged", True))

    rank_plans: list[RankPlan] = []
    for rank, launch in enumerate(ranks):
        metadata = ContainerMetadata(
            deployment=dep_id,
            recipe=str(recipe.get("id") or recipe_id),
            image=image_ref,
            mode=mode,
            created_at=created_at,
            memory_limit_gb=config.docker_memory_limit_gb,
            shm_size_gb=shm_size,
            privileged=privileged,
            # The identity travels on the metadata, so every container
            # service writes the same labels — reconciliation reads them
            # back rather than parsing the container name.
            generation=generation,
            rank=rank,
            world_size=topology.size,
        )
        rank_plans.append(
            RankPlan(
                rank=rank,
                # Empty means this machine, which is what a size-one
                # deployment has always carried.
                node=node_list[rank] if rank < len(node_list) else "",
                host=topology.nodes[rank].host,
                command=launch.command,
                script=launch.script,
                is_head=rank == 0,
                container=ContainerSpec(
                    image=image_ref,
                    name=rank_container_name(dep_id, rank, generation),
                    command=str(profile.get("keepalive") or "sleep infinity"),
                    env=_build_env(engine_obj, recipe, topology, node_rank=rank),
                    labels=metadata.to_labels(),
                    mounts=mounts,
                    privileged=privileged,
                    ipc_host=bool(profile.get("ipc_host", False)),
                    network_host=network_host,
                    shm_size_gb=shm_size,
                    devices=[str(d) for d in (profile.get("devices") or [])],
                    cap_add=[str(c) for c in (profile.get("cap_add") or [])],
                    ulimits={
                        str(k): str(v)
                        for k, v in (profile.get("ulimits") or {}).items()
                    },
                    memory_limit_gb=config.docker_memory_limit_gb,
                    pids_limit=config.docker_pids_limit,
                    nofile_limit=config.docker_nofile_limit,
                    # Only rank zero serves the API; a worker publishes
                    # nothing, and two ranks binding one host port collide.
                    port_mappings=list(port_mappings) if rank == 0 else [],
                    entrypoint_clear=not config.docker_keep_entrypoint,
                ),
            )
        )

    # The scalar container spec is rank zero's, by identity rather than by
    # copy, so every existing reader keeps working.
    container = rank_plans[0].container

    image_present, image_size = _inspect_image(image_ref, warnings)

    readiness = engine_obj.readiness_path()
    return DeployPlan(
        deployment_id=dep_id,
        recipe_id=str(recipe.get("id") or recipe_id),
        recipe_name=str(recipe.get("name") or recipe_id),
        name=name or str(recipe.get("name") or recipe_id),
        engine=engine_name,
        variant=resolved_variant,
        image_ref=image_ref,
        model=resolved_model,
        solo=topology.is_solo,
        nodes=node_list,
        node_count=topology.size,
        port=port,
        rendezvous_port=rendezvous_port,
        readiness_path=readiness,
        readiness_url=f"http://127.0.0.1:{port}{readiness}",
        metrics_path=engine_obj.metrics_path(),
        workdir=engine_obj.spec.runtime.workdir or "/workspace",
        mods=mods,
        params=merged,
        extra_args=list(extra_args or []),
        launch_command=ranks[0].command,
        ranks=[r.to_dict() for r in ranks],
        container=container,
        cache_mounts=cache_mounts,
        rank_plans=rank_plans,
        generation=generation,
        image_present=image_present,
        image_size_bytes=image_size,
        warnings=warnings,
    )


# ── Record shape ─────────────────────────────────────────────────────────────


def _record_from_plan(plan_obj: DeployPlan, status: str) -> dict[str, Any]:
    """The deployment record the UI and the upstream path both understand."""
    return {
        "id": plan_obj.deployment_id,
        "recipe_id": plan_obj.recipe_id,
        "name": plan_obj.name,
        "params": plan_obj.params,
        "nodes": plan_obj.nodes or None,
        "status": status,
        "created_at": plan_obj.created_at,
        "started_at": None,
        "stopped_at": None,
        "error_message": None,
        "pid": None,
        "port": plan_obj.port,
        "rendezvous_port": plan_obj.rendezvous_port,
        "launch_command": plan_obj.launch_command,
        "log_path": None,
        # Native additions.
        "runtime": RUNTIME_NAME,
        "engine": plan_obj.engine,
        "variant": plan_obj.variant,
        "image_ref": plan_obj.image_ref,
        "model": plan_obj.model,
        # Rank zero's name, kept as a scalar alias so every existing reader
        # — the health router, the UI, the upstream path — still resolves.
        "container_name": plan_obj.container.name,
        "image_present": plan_obj.image_present,
        "node_count": plan_obj.node_count,
        "mods": plan_obj.mods,
        "readiness_url": plan_obj.readiness_url,
        # Per-rank additions.
        "generation": plan_obj.generation,
        "ranks": [_rank_record(r) for r in plan_obj.rank_plans],
        "orphans": [],
    }


def _rank_record(rank_plan: RankPlan) -> dict[str, Any]:
    """The persisted shape of one rank."""
    return {
        "rank": rank_plan.rank,
        "node": rank_plan.node,
        "host": rank_plan.host,
        "container_name": rank_plan.container.name,
        "is_head": rank_plan.is_head,
    }


# ── Starting ─────────────────────────────────────────────────────────────────


def _resolve_mod_dir(mod: str) -> Path:
    """Locate a recipe's mod on disk.

    Two places, because mods come from two: a spark-vllm-docker checkout, where
    recipes name them relative to the root (``mods/fix-x``) or bare, and the
    operator's own ``custom-mods`` directory. The latter used to be reachable
    only because a ``mods/custom-x`` symlink was planted in the checkout; it is
    looked up directly now, so a custom mod works with no checkout at all.
    """
    candidates: list[Path] = []
    root = config.spark_vllm_dir
    if root is not None:
        candidates.append(root / mod)
        if not mod.startswith("mods/"):
            candidates.append(root / "mods" / mod)
    name = mod.removeprefix("mods/")
    custom_root = tools.custom_files.custom_mods_dir()
    candidates.append(custom_root / name)
    prefix = tools.custom_files.CUSTOM_PREFIX
    if name.startswith(prefix):
        candidates.append(custom_root / name.removeprefix(prefix))
    for candidate in candidates:
        if (candidate / "run.sh").is_file():
            return candidate
    raise NativeRuntimeError(
        f"mod '{mod}' has no run.sh; looked in " + ", ".join(str(c) for c in candidates)
    )


def _apply_mods(
    docker: Any,
    plan_obj: DeployPlan,
    container_name: str,
    warnings: list[str],
) -> list[str]:
    """Copy each mod into one rank's container and run its ``run.sh``.

    ``WORKSPACE_DIR`` is the image's working directory, as upstream sets it
    from ``$PWD``: mods drop files there and recipes reference them by bare
    name (``--chat-template unsloth.jinja``). Every rank runs the same mods —
    they patch the image's contents, and each rank has its own copy of it.
    """
    applied: list[str] = []
    workdir = plan_obj.workdir or "/workspace"
    for mod in plan_obj.mods:
        mod_dir = _resolve_mod_dir(mod)
        name = mod_dir.name
        remote = f"{MODS_DIR}/{name}"
        docker.exec_in_container(container_name, ["mkdir", "-p", remote])
        for path in sorted(mod_dir.iterdir()):
            # docker cp takes files and directories alike.
            docker.copy_to_container(container_name, str(path), f"{remote}/{path.name}")
        result = docker.exec_in_container(
            container_name,
            ["bash", "-lc", f"cd {remote} && WORKSPACE_DIR={workdir} bash run.sh"],
        )
        if not result.ok:
            raise NativeRuntimeError(
                f"mod '{mod}' failed (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )
        applied.append(mod)
    return applied


def _deploy_script(docker: Any, rank_plan: RankPlan) -> None:
    """Copy this rank's rendered script in and exec it detached.

    Output is redirected to PID 1's stdout so ``docker logs`` on that rank's
    container carries the serve output.
    """
    name = rank_plan.container.name
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", prefix="spark-pulse-", delete=False
    ) as handle:
        handle.write(rank_plan.script)
        local_path = handle.name
    try:
        if not docker.copy_to_container(name, local_path, SCRIPT_PATH):
            raise NativeRuntimeError(f"could not copy the launch script into {name}")
    finally:
        try:
            os.unlink(local_path)
        except OSError:  # pragma: no cover - defensive
            pass

    docker.exec_in_container(name, ["chmod", "+x", SCRIPT_PATH])
    docker.exec_in_container(
        name,
        ["bash", "-lc", f"bash {shlex.quote(SCRIPT_PATH)} >> /proc/1/fd/1 2>&1"],
        detach=True,
    )


def probe_ready(url: str, timeout: float = 3.0) -> bool:
    """Whether the engine answers its readiness endpoint.

    Simulation mode has no engine to answer, so the probe succeeds — the mock
    container service is already pretending the rest of the lifecycle worked.
    """
    import httpx

    if tools.is_simulation():
        return True

    try:
        response = httpx.get(url, timeout=timeout)
    except Exception:
        return False
    return response.status_code < 400


def _wait_ready(
    services: Callable[[str], Any],
    plan_obj: DeployPlan,
    timeout: int,
    interval: float = 2.0,
) -> None:
    """Poll rank zero's readiness, failing fast when any rank exits first.

    A worker that dies takes the gang with it, so every rank is watched even
    though only rank zero answers the readiness endpoint. A rank on a node we
    cannot reach is *not* evidence of death — the exception is swallowed and
    the deadline is what eventually decides.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for rank_plan in plan_obj.rank_plans:
            docker = services(rank_plan.node)
            name = rank_plan.container.name
            try:
                status = docker.get_container_status(name)
            except Exception as exc:  # pragma: no cover - transport specific
                logger.debug("could not check rank %s: %s", rank_plan.rank, exc)
                continue
            if not status.get("running"):
                logs = logs_for_container(docker, name, 50)
                raise NativeRuntimeError(
                    f"container {name} exited before the engine "
                    f"became ready ({status.get('status')}). "
                    f"Last log lines:\n{logs}"
                )
        if probe_ready(plan_obj.readiness_url):
            return
        time.sleep(interval)
    raise NativeRuntimeError(
        f"engine did not become ready within {timeout}s at " f"{plan_obj.readiness_url}"
    )


# ── Reaping, confirmation and teardown ───────────────────────────────────────


def _confirm_gone(
    docker: Any,
    name: str,
    timeout: float | None = None,
    interval: float | None = None,
) -> bool:
    """Whether ``name`` is really gone, by looking rather than by assuming."""
    timeout = CONFIRM_GONE_TIMEOUT if timeout is None else timeout
    interval = CONFIRM_GONE_INTERVAL if interval is None else interval
    deadline = time.monotonic() + timeout
    while True:
        if docker.get_container_status(name).get("status") == "missing":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _reap(docker: Any, name: str, where: str) -> None:
    """Remove one leftover container and wait for the evidence it is gone."""
    logger.info("Reaping leftover container %s", name)
    docker.stop_container(name)
    if not _confirm_gone(docker, name):
        raise NativeRuntimeError(
            f"container {name} on {where or 'this machine'} did not go away "
            f"within {CONFIRM_GONE_TIMEOUT:g}s; a rank of an earlier attempt "
            "is still holding the GPU, so this one will not be started"
        )


def _stale_names(docker: Any, plan_obj: DeployPlan, rank_plan: RankPlan) -> list[str]:
    """Containers of this deployment on this node that must not survive.

    Anything carrying the deployment label at a different generation is a
    leftover from an abandoned attempt. The name we are about to claim counts
    too: Docker's name reservation is the exactly-once primitive, and it only
    works if the name is free.
    """
    names: list[str] = []
    current = str(plan_obj.generation)
    try:
        containers = docker.list_managed_containers(
            {DEPLOYMENT_LABEL: plan_obj.deployment_id}
        )
    except Exception as exc:
        raise NativeRuntimeError(
            f"could not list containers on {rank_plan.node or 'this machine'} "
            f"to reap earlier attempts: {exc}"
        ) from exc
    for container in containers:
        labels = getattr(container, "labels", {}) or {}
        if labels.get(GENERATION_LABEL) == current:
            continue
        names.append(container.name)
    target = rank_plan.container.name
    if target not in names:
        try:
            present = docker.get_container_status(target).get("status") != "missing"
        except Exception:  # pragma: no cover - defensive
            present = False
        if present:
            names.append(target)
    return names


def _reap_earlier_generations(
    services: Callable[[str], Any], plan_obj: DeployPlan
) -> None:
    """Confirm every container of an earlier generation is gone.

    Nobody in the survey starts generation N+1 while a rank of generation N
    might still be alive: the model is sharded across exactly those ranks and
    the GPU is not shareable. Failing to get the evidence fails the deploy.
    """
    for rank_plan in plan_obj.rank_plans:
        docker = services(rank_plan.node)
        for name in _stale_names(docker, plan_obj, rank_plan):
            _reap(docker, name, rank_plan.node)


def _orphan(entry: dict[str, Any], reason: str) -> dict[str, Any]:
    """An outstanding rank: asked to stop, never confirmed gone."""
    return {
        "rank": entry.get("rank", 0),
        "node": entry.get("node", ""),
        "container_name": entry.get("container_name", ""),
        "reason": reason,
        "since": _now(),
    }


def _teardown_entry(docker: Any, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Stop one rank. Returns an orphan record when it cannot be confirmed."""
    name = str(entry.get("container_name") or "")
    if not name:
        return None
    try:
        docker.stop_container(name)
    except Exception as exc:
        logger.warning("Stopping %s failed: %s", name, exc)
        return _orphan(entry, f"the node could not be reached: {exc}")
    try:
        if _confirm_gone(docker, name):
            return None
    except Exception as exc:
        logger.warning("Could not confirm %s is gone: %s", name, exc)
        return _orphan(entry, f"removal could not be confirmed: {exc}")
    return _orphan(entry, "the container was still present after being stopped")


def _teardown_entries(
    services: Callable[[str], Any], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Tear ranks down head-first, collecting the ones left outstanding.

    Rank zero dies first so the rendezvous collapses instead of leaving the
    workers blocked on it for PyTorch's ``init_process_group`` timeout — ten
    minutes for NCCL, thirty for gloo. It is PyTorch's, not NCCL's: NCCL has
    no collective timeout of its own.
    """
    orphans: list[dict[str, Any]] = []
    for entry in sorted(entries, key=lambda e: int(e.get("rank", 0))):
        try:
            docker = services(str(entry.get("node") or ""))
        except Exception as exc:
            orphans.append(_orphan(entry, f"the node could not be reached: {exc}"))
            continue
        orphan = _teardown_entry(docker, entry)
        if orphan is not None:
            orphans.append(orphan)
    return orphans


# ── Image pull ───────────────────────────────────────────────────────────────

# Deployments with a pull in flight, mapped to whether a teardown has asked it
# to stop. A pull is the one part of a deploy that runs for tens of minutes, so
# stop/delete has to be able to reach into it: without this the container
# service kept downloading a 26 GB image for a deployment that no longer
# existed, and the record only settled when the download finally finished.
_active_pulls: dict[str, bool] = {}
_pull_lock = threading.Lock()


def _register_pull(deployment_id: str) -> None:
    with _pull_lock:
        _active_pulls[deployment_id] = False


def _unregister_pull(deployment_id: str) -> None:
    with _pull_lock:
        _active_pulls.pop(deployment_id, None)


def pull_is_active(deployment_id: str) -> bool:
    """Whether an image pull for this deployment is running right now."""
    with _pull_lock:
        return deployment_id in _active_pulls


def cancel_pull(deployment_id: str) -> bool:
    """Ask an in-flight pull for this deployment to stop. False if none."""
    with _pull_lock:
        if deployment_id not in _active_pulls:
            return False
        _active_pulls[deployment_id] = True
        return True


def _pull_cancel_requested(deployment_id: str) -> bool:
    with _pull_lock:
        return _active_pulls.get(deployment_id, False)


def _pull_targets(plan_obj: DeployPlan) -> list[str]:
    """Each distinct node the gang runs on, in start order.

    Workers pull first for the same reason they start first: rank zero should
    be the last thing that has to wait.
    """
    seen: list[str] = []
    for rank_plan in plan_obj.start_order():
        if rank_plan.node not in seen:
            seen.append(rank_plan.node)
    return seen


def _image_missing(services: Callable[[str], Any], plan_obj: DeployPlan) -> bool:
    """Whether this deploy has to pull before it can start anything.

    True when *any* node the gang runs on lacks the image. A daemon that will
    not answer is not evidence the image is absent, so an unreachable check
    keeps the deploy on the inline path where the failure is reported straight
    back to the caller.
    """
    for address in _pull_targets(plan_obj):
        try:
            if not services(address).image_exists(plan_obj.container.image):
                return True
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("image presence check failed on %s: %s", address, exc)
    return False


def _pull_image_if_missing(docker: Any, plan_obj: DeployPlan) -> bool:
    """Pull the plan's image before the container is created, with progress.

    ``containers.run`` pulls implicitly and silently, so a deploy against an
    image the host lacks used to sit with no output for the tens of minutes a
    26 GB engine image takes. Doing it here makes the wait visible: the record
    goes to ``pulling`` and aggregated progress events flow over SSE.

    Returns True when a pull actually ran. Raises :class:`NativeRuntimeError`
    when the pull fails, or :class:`PullCancelled` when a teardown stopped it.
    """
    dep_id = plan_obj.deployment_id
    ref = plan_obj.container.image
    try:
        if docker.image_exists(ref):
            return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("image presence check failed for %s: %s", ref, exc)

    _update_record(dep_id, status="pulling")
    publish_event(
        EventType.IMAGE_PULL_STARTED,
        dep_id,
        f"pulling {ref}",
        {"image_ref": ref, "percent": 0.0},
    )

    def _progress(snapshot: dict[str, Any]) -> None:
        publish_event(
            EventType.IMAGE_PULL_PROGRESS,
            dep_id,
            f"pulling {ref}: {snapshot.get('percent', 0)}%",
            {"image_ref": ref, **snapshot},
        )

    _register_pull(dep_id)
    try:
        result = docker.pull_image(
            ref, _progress, cancel=lambda: _pull_cancel_requested(dep_id)
        )
    except PullCancelled:
        publish_event(
            EventType.IMAGE_PULL_CANCELLED,
            dep_id,
            f"pull of {ref} cancelled",
            {"image_ref": ref},
        )
        raise
    except Exception as exc:
        message = f"could not pull image {ref}: {exc}"
        publish_event(EventType.IMAGE_PULL_FAILED, dep_id, message, {"image_ref": ref})
        raise NativeRuntimeError(message) from exc
    finally:
        _unregister_pull(dep_id)

    publish_event(
        EventType.IMAGE_PULL_COMPLETED,
        dep_id,
        f"pulled {ref}",
        {"image_ref": ref, **(result if isinstance(result, dict) else {})},
    )
    _update_record(dep_id, status="starting", image_present=True)
    return True


def persist_planned_record(plan_obj: DeployPlan, status: str) -> dict[str, Any]:
    """Write the plan's record at ``status``, replacing any earlier one.

    Split out of :func:`start` so a deploy that has to pull first can put the
    record on disk in ``pulling`` *before* the POST returns, rather than the
    caller seeing nothing until a 26 GB download finishes.
    """
    record = _record_from_plan(plan_obj, status)
    records = [r for r in _load_records() if r.get("id") != plan_obj.deployment_id]
    records.append(record)
    _save_records(records)
    return record


def _create_rank(
    docker: Any,
    plan_obj: DeployPlan,
    rank_plan: RankPlan,
    warnings: list[str],
) -> None:
    """Create one rank's idle container and apply the mods to it.

    Nothing is *launched* here — that is :func:`_launch_rank`, and the split
    is upstream's. ``launch-cluster.sh`` runs every container first (line
    1097 for the head, 1106 for each worker), applies mods to all of them
    (1111-1121), and only then execs the serve command (1201-1242). Doing it
    in one pass per rank instead would have rank one already rendezvousing
    while rank zero's image turns out to be missing.

    Raises :class:`NativeRuntimeError` with an explained reason. The caller
    tears the whole gang down on any failure; nothing is retried per rank,
    because the model is sharded across exactly these ranks.
    """
    dep_id = plan_obj.deployment_id
    spec = rank_plan.container
    where = rank_plan.node or "this machine"

    # Bind sources have to exist before the container does, or docker creates
    # them owned by root and every later write to the HF cache fails.
    if spec.mounts:
        try:
            unmade = docker.ensure_directories(sorted(spec.mounts))
        except Exception as exc:  # pragma: no cover — best effort
            logger.debug("could not create mount sources on %s: %s", where, exc)
            unmade = []
        if unmade:
            warnings.append(
                f"could not create {', '.join(unmade)} on {where}; docker will "
                "create them as root, which breaks later writes to the cache"
            )

    try:
        docker.run_container(
            image=spec.image,
            name=spec.name,
            env_vars=spec.env,
            metadata=ContainerMetadata.from_labels(spec.labels),
            privileged=spec.privileged,
            memory_limit_gb=spec.memory_limit_gb,
            shm_size_gb=spec.shm_size_gb,
            pids_limit=spec.pids_limit,
            nofile_limit=spec.nofile_limit,
            port_mappings=spec.port_mappings or None,
            entrypoint_clear=spec.entrypoint_clear,
            command=spec.command,
            mounts=spec.mounts,
            network_host=spec.network_host,
            ipc_host=spec.ipc_host,
            devices=spec.devices,
            cap_add=spec.cap_add,
            ulimits=spec.ulimits,
            auto_remove=False,
        )
    except Exception as exc:
        raise NativeRuntimeError(
            f"could not start container {spec.name}: {exc}"
        ) from exc

    publish_event(
        EventType.DEPLOYMENT_CONTAINER_STARTED,
        dep_id,
        f"container {spec.name} started from {spec.image}",
        {
            "container_name": spec.name,
            "image_ref": spec.image,
            "rank": rank_plan.rank,
            "node": rank_plan.node,
        },
    )

    applied = _apply_mods(docker, plan_obj, spec.name, warnings)
    if plan_obj.mods:
        publish_event(
            EventType.DEPLOYMENT_MODS_APPLIED,
            dep_id,
            f"applied {len(applied)}/{len(plan_obj.mods)} mod(s)",
            {"mods": applied, "warnings": warnings, "rank": rank_plan.rank},
        )


def _launch_rank(docker: Any, plan_obj: DeployPlan, rank_plan: RankPlan) -> None:
    """Exec one rank's rendered script in the container already created for it."""
    _deploy_script(docker, rank_plan)
    logger.info(
        "rank %s of %s is running on %s",
        rank_plan.rank,
        plan_obj.deployment_id,
        rank_plan.node or "this machine",
    )


def start(
    plan_obj: DeployPlan,
    docker: Any | None = None,
    wait: bool = True,
    ready_timeout: int | None = None,
    initial_status: str = "starting",
    services: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Run the plan, one rank at a time: workers first, rank zero last.

    Failure is all-or-nothing. Any rank that will not come up tears down the
    ranks already started — head first — and fails the deployment, naming the
    rank and the cause. There is no partial state and no per-rank restart.

    At one node every loop here has length one, so this is the single-container
    lifecycle that ran before ranks existed.
    """
    services = services or rank_services(docker)
    dep_id = plan_obj.deployment_id
    spec = plan_obj.container
    warnings = list(plan_obj.warnings)

    record = persist_planned_record(plan_obj, initial_status)

    publish_event(
        EventType.DEPLOYMENT_PLANNED,
        dep_id,
        f"planned {plan_obj.recipe_id} on {plan_obj.engine}/{plan_obj.variant}",
        {
            "image_ref": plan_obj.image_ref,
            "model": plan_obj.model,
            "port": plan_obj.port,
            "node_count": plan_obj.node_count,
            "generation": plan_obj.generation,
        },
    )

    def _fail(message: str, orphans: list[dict[str, Any]] | None = None) -> dict:
        publish_event(EventType.DEPLOYMENT_ERROR, dep_id, message)
        updated = _update_record(
            dep_id,
            status="error",
            error_message=message,
            stopped_at=_now(),
            orphans=orphans or [],
        )
        return updated or {**record, "status": "error", "error_message": message}

    # Nothing of an earlier attempt may still be alive when this one claims
    # the names and the GPUs.
    try:
        _reap_earlier_generations(services, plan_obj)
    except NativeRuntimeError as exc:
        return _fail(str(exc))

    try:
        for address in _pull_targets(plan_obj):
            _pull_image_if_missing(services(address), plan_obj)
    except PullCancelled:
        # A stop or delete reached into the pull. That is not a failure to
        # report: the record is already being torn down, and marking it
        # "error" would leave a deliberate teardown looking like a crash.
        message = f"pull of {spec.image} cancelled by teardown"
        publish_event(EventType.DEPLOYMENT_STOPPED, dep_id, message)
        return _update_record(dep_id, status="stopped", stopped_at=_now()) or {
            **record,
            "status": "stopped",
        }
    except NativeRuntimeError as exc:
        return _fail(str(exc))

    # Two phases, as upstream has them. Every container is created and
    # modded before any of them is launched, so an image that is missing on
    # rank zero surfaces before rank one has joined a rendezvous; then the
    # workers are launched and rank zero last, so nobody is left in a
    # store-connect timeout waiting for a head that never started.
    touched: list[RankPlan] = []

    def _abort(rank_plan: RankPlan, exc: Exception, phase: str) -> dict[str, Any]:
        # The rank that failed may itself have a container: run_container can
        # succeed and a mod or the script copy fail after it.
        pending = [*touched] if rank_plan in touched else [*touched, rank_plan]
        orphans = _teardown_entries(services, [_rank_record(r) for r in pending])
        return _fail(
            f"rank {rank_plan.rank} of {plan_obj.node_count} on "
            f"{rank_plan.node or 'this machine'} failed to {phase}, so the "
            f"whole deployment was torn down: {exc}",
            orphans,
        )

    for rank_plan in plan_obj.teardown_order():
        try:
            _create_rank(services(rank_plan.node), plan_obj, rank_plan, warnings)
        except NativeRuntimeError as exc:
            return _abort(rank_plan, exc, "start")
        touched.append(rank_plan)

    for rank_plan in plan_obj.start_order():
        try:
            _launch_rank(services(rank_plan.node), plan_obj, rank_plan)
        except NativeRuntimeError as exc:
            return _abort(rank_plan, exc, "launch")

    started = _now()
    _update_record(dep_id, status="starting", started_at=started, warnings=warnings)
    publish_event(
        EventType.DEPLOYMENT_SERVING,
        dep_id,
        f"launch script running in {spec.name}",
        {"command": plan_obj.launch_command},
    )

    if not wait:
        return _update_record(dep_id, status="running") or record

    timeout = ready_timeout or config.deploy_ready_timeout_seconds
    try:
        _wait_ready(services, plan_obj, timeout)
    except NativeRuntimeError as exc:
        return _fail(str(exc))

    publish_event(
        EventType.DEPLOYMENT_READY,
        dep_id,
        f"{plan_obj.recipe_id} is serving on port {plan_obj.port}",
        {"port": plan_obj.port, "readiness_url": plan_obj.readiness_url},
    )
    return _update_record(dep_id, status="running", error_message=None) or record


def create_deployment(
    recipe_id: str,
    name: str = "",
    params: dict[str, Any] | None = None,
    nodes: list[str] | None = None,
    engine: str | None = None,
    variant: str | None = None,
    model: str | None = None,
    extra_args: list[str] | None = None,
    allow_missing_model: bool = False,
    wait: bool = False,
) -> dict[str, Any]:
    """Plan and start in one call — the shape the deployments router wants.

    Nothing slow runs on the caller's thread. When the image is already here,
    the container is started inline (milliseconds) and only readiness is
    awaited in the background. When it is not, the whole start — pull included
    — moves to a background thread and the record is written as ``pulling``
    first, so the POST returns immediately instead of holding one of the
    process's forty worker threads for the tens of minutes a 26 GB engine
    image takes. Either way the UI follows the rest over SSE.
    """
    plan_obj = plan(
        recipe_id,
        engine=engine,
        variant=variant,
        model=model,
        params=params or {},
        extra_args=extra_args or [],
        nodes=nodes,
        solo=not nodes,
        name=name,
        allow_missing_model=allow_missing_model,
    )
    if wait:
        return start(plan_obj, wait=True)

    services = rank_services()

    def _watch() -> None:
        try:
            _wait_ready(services, plan_obj, config.deploy_ready_timeout_seconds)
        except NativeRuntimeError as exc:
            publish_event(EventType.DEPLOYMENT_ERROR, plan_obj.deployment_id, str(exc))
            _update_record(
                plan_obj.deployment_id,
                status="error",
                error_message=str(exc),
                stopped_at=_now(),
            )
            return
        publish_event(
            EventType.DEPLOYMENT_READY,
            plan_obj.deployment_id,
            f"{plan_obj.recipe_id} is serving on port {plan_obj.port}",
            {"port": plan_obj.port},
        )

    if _image_missing(services, plan_obj):
        record = persist_planned_record(plan_obj, "pulling")

        def _pull_then_start() -> None:
            started = start(
                plan_obj, services=services, wait=False, initial_status="pulling"
            )
            if started.get("status") in ("error", "stopped"):
                return
            _watch()

        threading.Thread(
            target=_pull_then_start,
            name=f"native-deploy-{plan_obj.deployment_id}",
            daemon=True,
        ).start()
        return record

    record = start(plan_obj, services=services, wait=False)
    if record.get("status") == "error":
        return record

    threading.Thread(
        target=_watch, name=f"native-ready-{plan_obj.deployment_id}", daemon=True
    ).start()
    return record


# ── Lifecycle ────────────────────────────────────────────────────────────────


def stop_deployment(
    deployment_id: str,
    docker: Any | None = None,
    services: Callable[[str], Any] | None = None,
) -> dict[str, Any] | None:
    """Stop every rank, head first, and mark the record stopped.

    Ranks whose removal could not be confirmed — a node that would not answer
    — are written back as outstanding orphans, and the deployment keeps its
    ports until something confirms those containers are gone.
    """
    record = get_deployment(deployment_id)
    if record is None:
        return None
    # A deployment still pulling has no container to stop; what has to stop is
    # the download. Ask first, then fall through — the pull thread settles the
    # record itself once it notices.
    cancel_pull(deployment_id)
    services = services or rank_services(docker)
    entries = rank_entries(record)
    orphans = _teardown_entries(services, entries)
    names = ", ".join(str(e.get("container_name") or "") for e in entries)
    publish_event(EventType.DEPLOYMENT_STOPPED, deployment_id, f"stopped {names}")
    return _update_record(
        deployment_id, status="stopped", stopped_at=_now(), orphans=orphans
    )


def delete_deployment(
    deployment_id: str,
    docker: Any | None = None,
    services: Callable[[str], Any] | None = None,
) -> bool:
    """Stop (if needed) and drop the record.

    A rank we could not confirm gone keeps the record: dropping it would free
    that node's ports on inference, which is exactly the orphan bug §3.3 warns
    about. The record stays, stopped, with its orphans listed.
    """
    record = get_deployment(deployment_id)
    if record is None:
        return False
    # Always tear the containers down, whatever the record says: a deployment
    # that errored during readiness still has a container running.
    stopped = stop_deployment(deployment_id, docker=docker, services=services)
    if stopped is not None and stopped.get("orphans"):
        return False
    records = _load_records()
    remaining = [r for r in records if r.get("id") != deployment_id]
    if len(remaining) == len(records):
        return False
    _save_records(remaining)
    return True


def logs_for_container(docker: Any, name: str, lines: int) -> str:
    try:
        return docker.get_logs(name, tail=lines)
    except Exception as exc:  # pragma: no cover - defensive
        return f"Failed to read container logs: {exc}"


def get_logs(
    deployment_id: str,
    lines: int = 200,
    docker: Any | None = None,
    rank: int = 0,
    services: Callable[[str], Any] | None = None,
) -> str:
    """``docker logs`` for one rank's container. Rank zero by default."""
    record = get_deployment(deployment_id)
    if record is None:
        return "Deployment not found"
    services = services or rank_services(docker)
    entry = next(
        (e for e in rank_entries(record) if int(e.get("rank", 0)) == rank), None
    )
    if entry is None:
        return f"Deployment has no rank {rank}"
    name = str(entry.get("container_name") or container_name_for(deployment_id))
    try:
        service = services(str(entry.get("node") or ""))
    except Exception as exc:
        return f"Failed to reach {entry.get('node') or 'this machine'}: {exc}"
    return logs_for_container(service, name, lines) or "(empty log)"


def _rank_status(
    services: Callable[[str], Any], entry: dict[str, Any]
) -> dict[str, Any]:
    """One rank's container state, with unreachable reported as unknown.

    "We could not ask" is not "it is dead": the third state exists precisely
    so a node we cannot reach does not read as a failure.
    """
    name = str(entry.get("container_name") or "")
    try:
        container = services(str(entry.get("node") or "")).get_container_status(name)
    except Exception as exc:
        container = {
            "status": "unknown",
            "running": False,
            "id": None,
            "state": {},
            "error": str(exc),
        }
    return {**entry, "container": container}


def status(
    deployment_id: str,
    docker: Any | None = None,
    services: Callable[[str], Any] | None = None,
) -> dict[str, Any] | None:
    """Live status: per-rank container state plus a readiness probe.

    ``container`` stays rank zero's, so every existing reader is unchanged;
    ``ranks`` carries the same thing for each rank.
    """
    record = get_deployment(deployment_id)
    if record is None:
        return None
    services = services or rank_services(docker)
    ranks = [_rank_status(services, entry) for entry in rank_entries(record)]
    container = ranks[0]["container"]
    port = record.get("port")
    url = record.get("readiness_url") or (f"http://127.0.0.1:{port}/v1/models")
    ready = bool(container.get("running")) and probe_ready(url)
    return {
        **record,
        "container": container,
        "ranks": ranks,
        "ready": ready,
        "status": _derive_status(record, container, ready),
    }


def _derive_status(
    record: dict[str, Any], container: dict[str, Any], ready: bool
) -> str:
    if record.get("status") in ("stopped", "error"):
        return str(record["status"])
    if not container.get("running"):
        return "stopped"
    return "running" if ready else "starting"


def list_deployments(docker: Any | None = None) -> list[dict[str, Any]]:
    """Native records, reconciled against the containers that actually exist.

    Container labels are the source of truth: a container we do not have a
    record for (server reinstalled, records lost) is adopted, and a record
    whose container is gone is marked stopped. Only ranks on the machine whose
    containers were enumerated count as evidence.

    The filter is the deployment label rather than ``mode=solo``. Filtering on
    the mode was invisible while every deployment was solo and wrong the
    moment one was not: a rank of a multi-node deployment carries
    ``mode=cluster``, so it was never enumerated, and a running deployment was
    marked stopped on the absence of a container the filter had excluded.
    """
    docker_arg = docker
    records = _load_records()
    native = [r for r in records if r.get("runtime") == RUNTIME_NAME]
    try:
        docker = docker or _docker_service()
        containers = docker.list_managed_containers({DEPLOYMENT_LABEL: ""})
    except Exception as exc:
        logger.debug("Native reconciliation skipped: %s", exc)
        return native

    by_name = {c.name: c for c in containers}
    known_ids = {r.get("id") for r in native}
    changed = False

    for record in native:
        if record.get("status") in ("stopped", "error"):
            continue
        # Only ranks on the machine we just enumerated produce evidence. A
        # rank on a node we did not ask about says nothing either way, and
        # marking the deployment stopped on that silence is the inference this
        # design refuses to make.
        for entry in rank_entries(record):
            if not _rank_is_here(entry):
                continue
            container = by_name.get(str(entry.get("container_name") or ""))
            if container is None or container.status not in ("running", "created"):
                record["status"] = "stopped"
                record.setdefault("stopped_at", _now())
                changed = True
                break

    for container in containers:
        if not container.name.startswith(CONTAINER_PREFIX):
            continue
        dep_id = container.labels.get(DEPLOYMENT_LABEL) or container.name.removeprefix(
            CONTAINER_PREFIX
        )
        if dep_id in known_ids:
            continue
        adopted = {
            "id": dep_id,
            "recipe_id": container.metadata.recipe,
            "name": container.metadata.recipe or dep_id,
            "params": {},
            "nodes": None,
            "status": "running" if container.status == "running" else "stopped",
            "created_at": container.metadata.created_at or _now(),
            "started_at": container.metadata.created_at,
            "stopped_at": None,
            "error_message": None,
            "pid": None,
            "port": None,
            "launch_command": "",
            "log_path": None,
            "runtime": RUNTIME_NAME,
            "engine": "",
            "variant": "",
            "image_ref": container.metadata.image or container.image,
            "model": "",
            "container_name": container.name,
            "node_count": int(container.labels.get(WORLD_SIZE_LABEL) or 1),
            "mods": [],
            "reconciled": True,
            "generation": int(container.labels.get(GENERATION_LABEL) or 0),
            "ranks": [
                {
                    "rank": int(container.labels.get(RANK_LABEL) or 0),
                    "node": "",
                    "host": "",
                    "container_name": container.name,
                    "is_head": (container.labels.get(RANK_LABEL) or "0") == "0",
                }
            ],
            "orphans": [],
        }
        native.append(adopted)
        records.append(adopted)
        changed = True

    if changed:
        _save_records(records)

    if docker_arg is None:
        # Only with the real resolver: asking this machine's daemon about a
        # container on another node would answer "missing" and free its ports
        # on nothing but our own ignorance.
        try:
            sweep_orphans()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("orphan sweep skipped: %s", exc)

    return native


def sweep_orphans(
    deployment_id: str | None = None,
    docker: Any | None = None,
    services: Callable[[str], Any] | None = None,
) -> int:
    """Drop orphans whose containers can now be confirmed gone.

    This is the other half of releasing on evidence: a node that was
    unreachable at teardown may answer later, and only its answer frees the
    ports the record has been holding. Returns how many orphans were cleared.
    """
    services = services or rank_services(docker)
    records = _load_records()
    cleared = 0
    changed = False
    for record in records:
        if deployment_id and record.get("id") != deployment_id:
            continue
        orphans = record.get("orphans") or []
        if not orphans:
            continue
        remaining: list[dict[str, Any]] = []
        for orphan in orphans:
            name = str(orphan.get("container_name") or "")
            try:
                gone = (
                    services(str(orphan.get("node") or "")).get_container_status(name)
                ).get("status") == "missing"
            except Exception as exc:
                logger.debug("orphan %s still unverifiable: %s", name, exc)
                gone = False
            if gone:
                cleared += 1
            else:
                remaining.append(orphan)
        if len(remaining) != len(orphans):
            record["orphans"] = remaining
            changed = True
    if changed:
        _save_records(records)
    return cleared
