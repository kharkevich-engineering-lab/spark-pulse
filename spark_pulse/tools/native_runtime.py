"""Native solo deployment runtime — Docker driven from Python.

This is the deploy path; there is no other. It runs the lifecycle the
native-runtime plan §1.4 sets out:

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
from concurrent.futures import ThreadPoolExecutor
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
from spark_pulse.tools.docker import (
    AGENT_USER,
    ContainerMetadata,
    PullCancelled,
    PullStalled,
)
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
#: Where the engine's home lives *inside* the container, and why it is not
#: ``/root`` any more.
#:
#: The container runs as the operator (:data:`ENGINE_USER`), so that everything
#: the engine downloads into the bind-mounted Hugging Face cache is owned by
#: the operator rather than by root. ``/root`` is mode 0700 in every engine
#: image, so a non-root process cannot even *traverse* it — a cache mounted at
#: ``/root/.cache/huggingface`` would be unreachable. A neutral home is what
#: makes the two decisions compatible: docker creates the missing path
#: components as mode 0755, and the leaves are the binds themselves.
CONTAINER_HOME = "/home/spark"
HF_CACHE_IN_CONTAINER = CONTAINER_HOME + "/.cache/huggingface"

#: Who the engine container runs as. Resolved on the node — see
#: :func:`spark_pulse.tools.docker.resolve_user` — because the control plane
#: cannot know a peer's uid.
#:
#: This is the fix for a failure that took a ``sudo chown -R`` to clear on a
#: real two-node cluster: the engine wrote the mounted hub cache as root, so
#: the control-plane user could no longer read ``trees/<rev>.json``, could not
#: take Hugging Face's ``.locks/`` lock on the next download, and replication
#: shipped 3.6 GB that the node-side verify then refused.
ENGINE_USER = AGENT_USER

#: The container's ``$HOME``, on the host. A root-owned home would be
#: traversable but not writable, and an engine writes more than its caches
#: there (``~/.config/vllm/usage_stats.json``, matplotlib's font cache). One
#: directory under the operator's own cache root makes the whole home theirs;
#: the specific caches bind on top of it.
ENGINE_HOME_ON_HOST = "~/.cache/spark-pulse/engine-home"

#: How long to wait for evidence that a container is really gone, and how
#: often to look. Removal is fast; the wait exists so the next generation
#: never races a rank that is still holding the GPU.
CONFIRM_GONE_TIMEOUT = 30.0
CONFIRM_GONE_INTERVAL = 0.5

#: How many ranks :func:`status` probes at once, and why the number is four.
#:
#: OpenSSH's ``MaxSessions`` defaults to 10 and ``sshd_config(5)`` defines it
#: as "the maximum number of open shell, login or subsystem sessions permitted
#: **per network connection**". We hold one multiplexed connection per node,
#: so the ceiling is per node, and it is a cliff rather than a slope: the
#: eleventh concurrent session on a connection is refused, ssh falls back to a
#: full handshake and logs ``ControlSocket … already exists, disabling
#: multiplexing``, after which *that connection* stops multiplexing for as
#: long as it stays saturated. Measured on a GB10: 59 ms at ten concurrent
#: inspects, 575 ms at twelve (``docs/rank-state-transport.md`` §1.2).
#:
#: Four is the bound that cannot cross it. It is charged against a single
#: node, not against the fleet, so it holds even in the case this code does
#: not otherwise defend against — every rank of a deployment landing on one
#: machine — and it still leaves six of that node's ten slots for the log
#: follows, event tails and an operator's own ssh that share the connection.
#: It is also :data:`~spark_pulse.engines.MAX_CLUSTER_NODES`, so the largest
#: cluster this hardware has a published topology for is probed in one wave.
RANK_STATUS_MAX_WORKERS = 4

#: Attached to every plan above one node, and to the record it becomes.
#:
#: Multi-node has run on two DGX Sparks (2026-09-17): vLLM tensor-parallel
#: across the ConnectX fabric, rendezvous, NCCL transport and interface pinning
#: all observed. What no run has yet measured — fabric bandwidth, three or four
#: nodes, SGLang across machines — is listed in ``web/src/lib/experimental.ts``
#: and ``docs/upstream-cluster-parity.md``; this is the one line that travels
#: with the plan itself.
MULTI_NODE_UNPROVEN = (
    "multi-node has run on two DGX Sparks with vLLM tensor-parallel over the "
    "ConnectX fabric; fabric bandwidth, three or four nodes and SGLang across "
    "machines have not yet been measured"
)


class NativeRuntimeError(RuntimeError):
    """A native deployment could not be planned or started."""


class MissingModelError(NativeRuntimeError):
    """The model a deployment needs is not in the local catalogue.

    A subclass, and not just a message, because this is the one planning
    failure with an obvious next step: fetch the model. Callers that can offer
    that need the model id as a value — parsing it back out of the sentence
    would be a contract nobody wrote down and the first reworded message would
    break it.
    """

    def __init__(self, model: str, message: str = "") -> None:
        self.model = model
        super().__init__(
            message
            or (
                f"model '{model}' is not in the local catalogue; "
                "download it first or deploy with allow_missing_model"
            )
        )


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
    #: Docker's ``--user``. :data:`ENGINE_USER` is the sentinel the node
    #: resolves to its own uid:gid; ``None`` would leave the image's root.
    user: str | None = ENGINE_USER

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
    #: For the head of an engine whose workers are the servers — llama.cpp
    #: over RPC, and nothing else today — one entry per worker: ``node`` (who),
    #: ``address``/``port`` (which wire), ``via_fabric`` and the ``reason``
    #: that address was chosen. Empty on every other rank and every other
    #: engine. The same list the ``--rpc`` flag was rendered from, so the
    #: preview cannot disagree with the command.
    rpc_endpoints: list[dict[str, Any]] = field(default_factory=list)

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
    #: The port every rank above zero listens on, when the engine spans nodes
    #: by having its workers serve rather than by forming a rendezvous —
    #: llama.cpp's RPC backend, and nothing else today. ``None`` at one node,
    #: where no worker exists to bind it.
    rpc_port: int | None
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
    #: Whether :attr:`model` is in the local catalogue. A plan permits a
    #: missing model; this is how the preview says so before the deploy
    #: that would not.
    model_present: bool = True
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
    """The container service for this machine — through its own agent.

    Not ``tools.docker`` directly. The control plane coordinates; the agent on
    each node executes, including the agent this process runs for itself and
    reaches over loopback. A solo deployment that drove Docker from Python
    while a two-node deployment drove it through an agent was two code paths
    for one operation, and only one of them was exercised by the machine most
    people run.
    """
    return tools.node_service.NodeServices().control()


def rank_services(docker: Any | None = None) -> Callable[[str], Any]:
    """Resolve the container service bound to a rank's node address.

    ``docker`` pins one service for every rank, which is what a caller that
    already holds a service (and every test) wants. Otherwise every address
    goes through the node-bound resolver — including the empty one, the
    record's long-standing sentinel for this machine, which
    ``is_local_address`` already reads as loopback. There is no local branch
    here: rank zero of a solo deployment reaches its container exactly the way
    rank one of a two-node deployment reaches its own.
    """
    if docker is not None:
        return lambda _address: docker

    resolver: Any = None

    def _resolve(address: str) -> Any:
        nonlocal resolver
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

    Upstream mounts ``~/.cache/vllm`` at ``/root/.cache/vllm``, because its
    container runs as root. Ours runs as the operator, so the home prefix is
    rewritten to :data:`CONTAINER_HOME` instead — same shape, a home the
    engine's uid can actually enter.
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

    Delegates to ``deployment_records.save``, which replaces the whole set in
    one database transaction — so a reader sees the previous set or the new
    one, never half of either.
    """
    tools.deployment_records.save(records)


def _update_record(deployment_id: str, **fields: Any) -> dict[str, Any] | None:
    """Change one record's fields, or answer None if it is no longer there.

    The whole load-modify-save runs under the record store's mutex. Without
    it, a background pull thread that had already loaded the list would write
    back a deployment the API thread deleted in the meantime — and the caller
    saw a successful delete followed by the record reappearing.
    """
    with tools.deployment_records.transaction():
        records = _load_records()
        for record in records:
            if record.get("id") == deployment_id:
                record.update(fields)
                _save_records(records)
                return record
        return None


#: What an error says when the code that wrote it did not. Nothing should
#: ever read this — a record that does is a bug with a name on it, which is
#: still better than the blank field it replaces.
UNEXPLAINED_ERROR = (
    "this deploy failed and nothing recorded why — please report it, and read "
    "the run's logs for the engine's own last words"
)


def _error_text(message: Any) -> str:
    """An error message that is never empty."""
    return str(message or "").strip() or UNEXPLAINED_ERROR


def _record_error(
    deployment_id: str, message: Any, **fields: Any
) -> dict[str, Any] | None:
    """Move a record to ``error`` — the one state that has to say why.

    Every path into ``error`` comes through here, and the same text goes onto
    the record and onto the ``DEPLOYMENT_ERROR`` frame, so the stream and the
    row cannot disagree about what happened. Twice on the cluster a record
    ended in ``error`` with ``error_message: null`` and the reason was only in
    ``docker logs``; an empty message is refused here rather than written.
    """
    text = _error_text(message)
    publish_event(EventType.DEPLOYMENT_ERROR, deployment_id, text)
    return _update_record(
        deployment_id,
        status="error",
        error_message=text,
        stopped_at=_now(),
        **fields,
    )


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
    # The image's own HOME is root's, and the container is not root's. Set
    # after the recipe's block env, so a recipe that names one still wins.
    env.setdefault("HOME", CONTAINER_HOME)
    token = config.hf_token
    if token:
        env["HF_TOKEN"] = token
    return env


def _home_relative(path: str) -> str:
    """``~``-relative when the path is under this user's home, as written.

    The inverse of :func:`_expand`, and it exists because a cache directory is
    sometimes read by another machine: the operator's home on a peer is that
    node's, not this one's, so a list meant to travel says ``~/.cache/vllm``
    and lets the far end expand it.
    """
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def _hf_home() -> str:
    try:
        return str(tools.models.hf_home())
    except Exception:  # pragma: no cover - defensive
        return _expand("~/.cache/huggingface")


def engine_cache_dirs(engine_obj: Engine | None = None) -> list[str]:
    """Every host directory a deploy bind-mounts into an engine container.

    With an ``engine_obj``, exactly what that engine declares (plus the two
    every engine gets: ``HF_HOME`` and the container's home). Without one, the
    union over every engine this control plane knows — which is what a check
    about what *past* deploys left behind has to look at, since the caches on
    a node were written by whichever engines have run there.

    This is the list :func:`_build_mounts` mounts from, deliberately: the
    doctor's ``engine-cache-ownership`` check reads it to decide which
    directories to look at, and a cache the deploy binds but the doctor never
    heard of is precisely the failure this exists to prevent.

    Returned as written — ``~``-relative under a home — because the reader may
    be another machine. Order is stable: declared caches first, then
    ``HF_HOME``, then the engine's home.
    """
    declared: list[str] = []
    if engine_obj is not None:
        declared.extend(engine_obj.cache_mounts())
    else:
        for spec in get_registry().list():
            declared.extend(spec.runtime.cache_mounts)
    declared.append(_home_relative(_hf_home()))
    declared.append(ENGINE_HOME_ON_HOST)

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in declared:
        portable = _home_relative(_expand(raw))
        if portable not in seen:
            seen.add(portable)
            ordered.append(portable)
    return ordered


def _build_mounts(engine_obj: Engine) -> tuple[dict[str, str], list[str]]:
    """Host->container bind mounts for the engine's caches plus ``HF_HOME``."""
    mounts: dict[str, str] = {}
    declared = list(engine_obj.cache_mounts())
    engine_home = _expand(ENGINE_HOME_ON_HOST)
    for raw in engine_cache_dirs(engine_obj):
        host = _expand(raw)
        # The home is the container's ``$HOME``, not a cache under it, so it
        # is pinned below rather than mapped by path like the others.
        if host != engine_home:
            mounts[host] = _container_path(host)
    hf_home = _hf_home()
    # HF_HOME wins over any engine-declared cache that targets the same path;
    # docker refuses two binds on one container destination.
    for host, target in list(mounts.items()):
        if target == HF_CACHE_IN_CONTAINER and host != hf_home:
            del mounts[host]
    mounts[hf_home] = HF_CACHE_IN_CONTAINER
    # The home itself, so ``$HOME`` is writable by the uid the engine runs as
    # rather than a root-owned directory docker invented for the binds beneath
    # it. Nested binds are fine — docker mounts them in path order.
    mounts.setdefault(engine_home, CONTAINER_HOME)
    return mounts, declared


def _engine_ulimits(declared: Any) -> dict[str, str]:
    """The engine's ulimits, plus the one running as the operator needs.

    A non-root process holds no ``CAP_IPC_LOCK`` even in a privileged
    container, and NCCL over RoCE registers pinned memory; unlimited
    ``memlock`` is the other way there. The bundled engine defaults set it,
    but an engine loaded from an OCI index may predate that — the cluster's
    vLLM did — and running the engine as the operator is this module's
    decision, so the limit that decision needs is added here, not left to
    every engine definition to remember. An engine that sets its own wins.
    """
    ulimits = {str(k): str(v) for k, v in (declared or {}).items()}
    ulimits.setdefault("memlock", "-1")
    return ulimits


def _bind_sources_to_create(mounts: dict[str, str]) -> list[str]:
    """Every host directory that must exist, as the operator, before the run.

    The bind sources themselves, and one more set that is easy to miss: the
    *destinations* nested inside the home bind. ``/home/spark`` is a bind of
    ``engine-home`` on the host, and ``/home/spark/.cache/huggingface`` is a
    bind beneath it. Docker mounts in path order, so when it reaches the
    nested one its destination is a path *inside engine-home* — and if that
    path is missing, docker creates it there, on the host, as root. Seen on
    the two-node cluster the first time the engine ran as the operator:
    ``engine-home/.cache`` came into being owned by root, and the engine
    could not create ``.cache/flashinfer`` under it (``PermissionError``),
    while every bind source was owned correctly. Creating the host-side path
    of each nested destination first means there is nothing left for docker
    to invent.
    """
    home_on_host = ""
    for host, target in mounts.items():
        if target == CONTAINER_HOME:
            home_on_host = host
    wanted = set(mounts)
    if home_on_host:
        prefix = CONTAINER_HOME + "/"
        for target in mounts.values():
            if target.startswith(prefix):
                wanted.add(home_on_host + target[len(CONTAINER_HOME) :])
    return sorted(wanted)


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

    resolved_model, model_present = _resolve_model(
        recipe, model, allow_missing_model, warnings
    )

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
    # Only above one node: at one node there is no worker to run an
    # rpc-server, so nothing binds it and it is not a port this deploy holds.
    rpc_port = engine_obj.rpc_port() if topology.size > 1 else None
    port = overrides.get("port") or defaults.get("port")
    if port in (None, "", 0):
        taken = _ports_in_use()
        if rendezvous_port:
            taken.add(rendezvous_port)
        if rpc_port:
            taken.add(rpc_port)
        port = allocate_port(taken)
        overrides["port"] = port
    port = int(port)
    for reserved, role in ((rendezvous_port, "rendezvous"), (rpc_port, "RPC")):
        if reserved and port == int(reserved):
            raise NativeRuntimeError(
                f"port {port} is the {role} port of engine "
                f"'{engine_name}/{resolved_variant}'; the launch binds it "
                "itself, so pick another API port"
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

    if engine_obj.parallelism_in_command:
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

    # Which wire the head will dial each worker on. Empty for the rendezvous
    # engines and for a solo launch; llama.cpp over RPC is the one that picks.
    rpc_endpoints = engine_obj.rpc_endpoints(topology)
    fell_back = [e for e in rpc_endpoints if not e.get("via_fabric")]
    if fell_back:
        warnings.append(
            "RPC traffic will take the registered address for "
            + ", ".join(str(e["node"]) for e in fell_back)
            + ", not the ConnectX fabric: "
            + "; ".join(str(e["reason"]) for e in fell_back)
            + ". Every activation tensor of every token crosses that link"
        )

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
                rpc_endpoints=list(rpc_endpoints) if rank == 0 else [],
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
                    ulimits=_engine_ulimits(profile.get("ulimits")),
                    memory_limit_gb=config.docker_memory_limit_gb,
                    pids_limit=config.docker_pids_limit,
                    nofile_limit=config.docker_nofile_limit,
                    # Only rank zero serves the API; a worker publishes
                    # nothing, and two ranks binding one host port collide.
                    # The exception is an engine whose workers are the servers
                    # (llama.cpp over RPC): the head connects to them from
                    # another machine, so off the host network their port has
                    # to be published or nothing can reach it.
                    port_mappings=(
                        list(port_mappings)
                        if rank == 0
                        else (
                            []
                            if network_host or not rpc_port
                            else [f"{rpc_port}:{rpc_port}"]
                        )
                    ),
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
        rpc_port=rpc_port,
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
        model_present=model_present,
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
        # When the engine was first *observed* serving. Not an inference from
        # a running container: the container runs a keepalive and outlives the
        # engine inside it, which is how a record read "running" in the list
        # and "starting" in the detail over an engine that had already died.
        "ready_at": None,
        "stopped_at": None,
        "error_message": None,
        "pid": None,
        "port": plan_obj.port,
        "rendezvous_port": plan_obj.rendezvous_port,
        "rpc_port": plan_obj.rpc_port,
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
        # The engine's Prometheus path, persisted so the metrics sampler can
        # address this deployment without re-resolving a spec that may since
        # have been withdrawn from the index. It was computed into the plan and
        # dropped here until there was a sampler to read it.
        "metrics_path": plan_obj.metrics_path,
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

    One directory — the operator's own ``custom-mods``. A recipe may name a
    mod the way upstream's format does (``mods/fix-x``), bare, or with the
    ``custom-`` prefix the listing shows; all three resolve to the same place,
    because that prefix is all the removed checkout symlinks ever added.
    """
    candidates: list[Path] = []
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

    These execs run as **root**, explicitly, even though the container's own
    user is the operator (:data:`ENGINE_USER`). A mod edits the image — a
    site-packages patch, a template dropped in a root-owned workdir — and has
    always done so as root; only the engine itself needs to be the operator,
    and only because of what it writes into the mounted cache.
    """
    applied: list[str] = []
    workdir = plan_obj.workdir or "/workspace"
    for mod in plan_obj.mods:
        mod_dir = _resolve_mod_dir(mod)
        name = mod_dir.name
        remote = f"{MODS_DIR}/{name}"
        docker.exec_in_container(container_name, ["mkdir", "-p", remote], user="root")
        for path in sorted(mod_dir.iterdir()):
            # docker cp takes files and directories alike.
            docker.copy_to_container(container_name, str(path), f"{remote}/{path.name}")
        result = docker.exec_in_container(
            container_name,
            ["bash", "-lc", f"cd {remote} && WORKSPACE_DIR={workdir} bash run.sh"],
            user="root",
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
    container carries the serve output. PID 1 is the keepalive, which runs as
    the container's user — the operator — so the engine can open its stdout;
    a root PID 1 under a non-root engine could not be written to at all.

    The copy lands as root (the daemon does the writing), so the mode is set
    to 0755 from root *before* the engine's exec: the launch reads the script
    as the operator, and a 0600 temporary file would be unreadable to it.
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

    docker.exec_in_container(name, ["chmod", "0755", SCRIPT_PATH], user="root")
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


#: How many log lines an error message quotes back. Twenty is a llama.cpp or
#: vLLM argument complaint plus the banner above it — enough to say why
#: without turning a record's ``error_message`` into a log file. The rest is
#: still there: a readiness failure does not tear the container down, so
#: ``GET /api/deployments/{id}/logs`` still has the whole log.
ERROR_LOG_LINES = 20

#: What the liveness probe exits with when it could not find out. Any code
#: that is neither "alive" (0) nor "no such process" (1) means the same
#: thing; this is the one the probe chooses when the image has no ``pgrep``.
PROBE_CANNOT_TELL = 111

#: A bound on the probe, which is one ``pgrep`` in a container that is up.
LIVENESS_PROBE_TIMEOUT = 10

#: Characters an ERE gives a meaning to. ``pgrep -f`` takes an ERE and every
#: pattern here is a literal — a path, a program name.
_ERE_SPECIAL = frozenset(".[]\\()*+?{}|^$")

#: Leading words that are not the program: ``env`` and its friends, and the
#: ``VAR=value`` assignments a recipe may prefix its command with.
_COMMAND_WRAPPERS = frozenset({"env", "exec", "nohup", "setsid", "stdbuf", "time"})


def _ere_literal(text: str) -> str:
    """``text`` as an ERE that matches nothing but itself."""
    return "".join("\\" + ch if ch in _ERE_SPECIAL else ch for ch in text)


def _self_excluding_pattern(text: str) -> str:
    """An ERE matching ``text`` in *another* process's command line.

    ``pgrep -f`` reads every command line in the namespace, and the shell
    asking the question carries the pattern in its own — so a naive probe
    finds itself and every serve process is alive forever. The first character
    goes into a bracket expression: ``[l]lama-server`` matches
    ``llama-server`` and does not match the literal ``[l]lama-server`` that
    the probe's own command line holds.

    A first character a bracket expression cannot carry falls back to the
    plain literal, which self-matches — a probe that says "alive" when it
    cannot tell, which is the direction that fails no deploy wrongly.
    """
    if not text:
        return ""
    head, rest = text[0], text[1:]
    if not (head.isalnum() or head in "/_-"):
        return _ere_literal(text)
    return f"[{head}]{_ere_literal(rest)}"


def serve_program(command: str) -> str:
    """The program a rendered launch command actually runs.

    ``pgrep -f`` matches a command line, and an engine's command line starts
    with its program: ``llama-server``, ``ggml-rpc-server``, ``vllm``. Leading
    environment assignments are skipped because they are not *in* a command
    line at all — they are the environment — and a probe looking for
    ``NCCL_DEBUG=INFO`` would find nothing and call a healthy engine dead.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:  # an unbalanced quote in a recipe's template
        tokens = command.split()
    for token in tokens:
        if token.startswith("-"):
            continue
        if "=" in token and not token.startswith("/"):
            continue
        if token in _COMMAND_WRAPPERS:
            continue
        return token
    return ""


def serve_process_alive(docker: Any, name: str, command: str) -> bool | None:
    """Whether the process the launch script exec'd is still in the container.

    The serve command is an ``exec`` inside a keepalive container, so the
    container outlives it. On the two-node cluster ``llama-server`` exited at
    t=0.4s on a flag llama.cpp had removed, the container stayed ``running``
    with nothing listening on the port, and the only thing that eventually
    noticed was the readiness deadline — minutes later, with no reason
    attached. This is what notices within one poll instead.

    The question is asked on the node, through that node's container service,
    as one ``pgrep -f`` per poll. Two patterns, because bash may or may not
    have replaced itself with the engine by the time we look: the launch
    script's own path, and the program the script runs.

    Three answers, and the third is the point. ``True`` is alive, ``False`` is
    *no such process* — evidence, not inference — and ``None`` is a probe that
    could not answer: an image without ``pgrep``, a node that stopped
    replying, an agent that refused the exec. Only ``False`` fails a deploy.
    """
    patterns = [p for p in (_self_excluding_pattern(SCRIPT_PATH),) if p]
    program = serve_program(command)
    if program:
        patterns.append(_self_excluding_pattern(program))
    script = f"command -v pgrep >/dev/null 2>&1 || exit {PROBE_CANNOT_TELL}\n"
    script += "".join(
        f"pgrep -f {shlex.quote(pattern)} >/dev/null 2>&1 && exit 0\n"
        for pattern in patterns
    )
    script += "exit 1\n"
    try:
        result = docker.exec_in_container(
            name, ["bash", "-lc", script], timeout=LIVENESS_PROBE_TIMEOUT
        )
    except Exception as exc:
        logger.debug("could not probe the serve process in %s: %s", name, exc)
        return None
    code = getattr(result, "returncode", None)
    if code == 0:
        return True
    if code == 1:
        return False
    logger.debug("the serve-process probe in %s could not answer (%s)", name, code)
    return None


#: How long a head waits for its RPC workers to start listening, and how
#: often it asks. Ninety seconds is a container start plus a binary that binds
#: a socket before it does anything else; a second between asks is cheap
#: against the alternative, which is a run that quietly uses one GPU.
RPC_READY_TIMEOUT = 90.0
RPC_READY_INTERVAL = 1.0
#: A worker one hop away either answers this connect or is not up yet.
RPC_PROBE_TIMEOUT = 2.0


def rpc_listening(address: str, port: int, timeout: float = RPC_PROBE_TIMEOUT) -> bool:
    """Whether a worker's RPC server is accepting connections yet.

    The *registered* address is probed, not the fabric one the head will dial:
    the control plane can always reach a node at the address it is registered
    at — that is what registration means — while the fabric is a wire between
    two peers that this machine may not be on at all. What is being answered
    here is "has ``ggml-rpc-server`` bound its socket", and a bound socket is
    bound on every interface (``-H 0.0.0.0``).

    Simulation has no server to bind anything, and the mock container service
    is already pretending the rest of the lifecycle worked, so the probe
    succeeds there — the same short-circuit :func:`probe_ready` makes.
    """
    if tools.is_simulation():
        return True
    try:
        with socket.create_connection((address, int(port)), timeout=timeout):
            return True
    except OSError as exc:
        logger.debug("RPC port %s on %s is not open yet: %s", port, address, exc)
        return False


def _wait_rpc_workers(
    services: Callable[[str], Any],
    plan_obj: DeployPlan,
    timeout: float | None = None,
    interval: float | None = None,
    probe: Callable[[str, int], bool] | None = None,
) -> None:
    """Hold rank zero back until every worker's RPC port answers.

    Ordering is not enough, and a real two-node Bonsai run is what says so:
    the worker container and the head started in the same second, the head
    dialled ``--rpc …:50052`` 0.27s later, got *Failed to connect* for every
    endpoint — and llama.cpp **carried on**. An unreachable RPC device is an
    absent device to it, so the model loaded onto the head's own GPU and
    served from one machine while the record said two. Nothing failed, which
    is the whole problem: the only evidence was a token rate.

    So the gang's head is launched against evidence rather than against a
    sequence. A worker that never listens fails the deploy, named, instead of
    becoming a run that is quietly half the cluster. A worker whose container
    has already exited fails immediately — waiting out the deadline for a
    container that is gone tells nobody anything.

    Only for an engine whose workers are the servers: no
    :attr:`DeployPlan.rpc_port`, nothing to wait for.
    """
    port = plan_obj.rpc_port
    if not port or plan_obj.node_count <= 1:
        return
    timeout = RPC_READY_TIMEOUT if timeout is None else timeout
    interval = RPC_READY_INTERVAL if interval is None else interval
    check = probe or rpc_listening
    pending = [r for r in plan_obj.rank_plans if not r.is_head]
    deadline = time.monotonic() + timeout
    while True:
        waiting: list[RankPlan] = []
        for rank_plan in pending:
            address = rank_plan.node or rank_plan.host
            if check(address, int(port)):
                logger.info(
                    "rank %s of %s is listening on %s:%s",
                    rank_plan.rank,
                    plan_obj.deployment_id,
                    address,
                    port,
                )
                continue
            docker = services(rank_plan.node)
            name = rank_plan.container.name
            try:
                status = docker.get_container_status(name)
            except Exception as exc:  # a node that stopped answering
                logger.debug("could not check rank %s: %s", rank_plan.rank, exc)
                status = {}
            if status and not status.get("running"):
                raise NativeRuntimeError(
                    f"container {name} on {rank_plan.node or 'this machine'} "
                    f"exited before it began serving RPC on port {port} "
                    f"({status.get('status')}). Last log lines:\n"
                    f"{logs_for_container(docker, name, 50)}"
                )
            waiting.append(rank_plan)
        if not waiting:
            return
        pending = waiting
        if time.monotonic() >= deadline:
            named = ", ".join(
                f"{r.node or r.host} (rank {r.rank})"
                for r in sorted(pending, key=lambda r: r.rank)
            )
            raise NativeRuntimeError(
                f"{named} did not accept a connection on RPC port {port} "
                f"within {timeout:g}s, so rank zero was not started. llama.cpp "
                "treats an RPC device it cannot reach as one that is not "
                "there: a head launched now would load the whole model onto "
                "its own GPU and serve from one machine while this record "
                "said several. Check that the worker's container is running "
                f"and that port {port} is reachable from the control plane"
            )
        time.sleep(interval)


def _exit_code_of(status: dict[str, Any]) -> int | None:
    """Docker's exit code for a container that has stopped, when it gave one."""
    state = status.get("state")
    if not isinstance(state, dict):
        return None
    code = state.get("ExitCode", state.get("exit_code"))
    return code if isinstance(code, int) else None


def _log_tail(docker: Any, name: str) -> str:
    """The container's last words, for an error that would otherwise have none."""
    return logs_for_container(docker, name, ERROR_LOG_LINES)


def _container_exit_message(
    docker: Any, rank_plan: RankPlan, status: dict[str, Any]
) -> str:
    name = rank_plan.container.name
    code = _exit_code_of(status)
    coda = f" with exit code {code}" if code is not None else ""
    return (
        f"container {name} on {rank_plan.node or 'this machine'} "
        f"exited before the engine became ready{coda} "
        f"({status.get('status')}). Last log lines:\n{_log_tail(docker, name)}"
    )


def _serve_process_gone_message(docker: Any, rank_plan: RankPlan) -> str:
    name = rank_plan.container.name
    program = serve_program(rank_plan.command) or "the engine"
    return (
        f"the serve process in {name} on {rank_plan.node or 'this machine'} "
        f"is gone: {program} exited before the engine became ready, and the "
        "container is still up because what it runs is a keepalive — the "
        f"engine is an exec inside it. Last log lines:\n{_log_tail(docker, name)}"
    )


def _readiness_deadline_message(
    services: Callable[[str], Any], plan_obj: DeployPlan, timeout: int
) -> str:
    """Why the wait ended, with the engine's own last words attached.

    A record that ends in ``error`` saying only that a deadline passed sends
    the operator to ``docker logs`` on a machine they may not be sitting at.
    Twice on the cluster that was the whole diagnosis — a refused flag, a
    cache it could not write — and both were in the log the whole time.
    """
    head = plan_obj.head
    message = (
        f"engine did not become ready within {timeout}s at " f"{plan_obj.readiness_url}"
    )
    try:
        tail = _log_tail(services(head.node), head.container.name)
    except Exception as exc:  # pragma: no cover - a node that stopped answering
        logger.debug("could not read the logs of %s: %s", head.container.name, exc)
        return message
    return f"{message}. Last log lines:\n{tail}" if tail.strip() else message


def _wait_ready(
    services: Callable[[str], Any],
    plan_obj: DeployPlan,
    timeout: int,
    interval: float = 2.0,
) -> None:
    """Poll rank zero's readiness, failing fast when a rank stops serving.

    There are two ways to stop serving and until the Bonsai run only one was
    watched. A rank's *container* exiting was. The serve process inside it
    exiting was not — the engine is an ``exec`` in a keepalive container, and
    the keepalive does not care — so a removed ``--draft-max`` killed
    ``llama-server`` at t=0.4s and the deploy sat there until the readiness
    deadline, minutes later, reporting a timeout against a container that had
    been empty the whole time. Both are checked now, every poll.

    A worker that dies takes the gang with it, so every rank is watched even
    though only rank zero answers the readiness endpoint. A rank on a node we
    cannot reach is *not* evidence of death: the status read's exception is
    swallowed, :func:`serve_process_alive` answers ``None`` rather than
    ``False``, and the deadline is what eventually decides.
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
                raise NativeRuntimeError(
                    _container_exit_message(docker, rank_plan, status)
                )
            if serve_process_alive(docker, name, rank_plan.command) is False:
                raise NativeRuntimeError(_serve_process_gone_message(docker, rank_plan))
        if probe_ready(plan_obj.readiness_url):
            return
        time.sleep(interval)
    raise NativeRuntimeError(_readiness_deadline_message(services, plan_obj, timeout))


# ── Reaping, confirmation and teardown ───────────────────────────────────────


def _is_confirmed_gone(container: dict[str, Any]) -> bool:
    """Whether a container status is *evidence* the container is not there.

    Only ``missing`` is, and ``missing`` now means what it says: a daemon
    answered and told us the object does not exist
    (:meth:`~spark_pulse.tools.node_service.RemoteNodeService.get_container_status`).
    ``unknown`` — a daemon that did not answer, a node we could not reach, a
    reply we could not parse — is not evidence of anything, and the two
    callers of this predicate both release a resource on a True: one starts a
    new generation on the GPU, the other frees the ports an orphan record was
    holding. Releasing on inference rather than on evidence is the failure the
    orphan machinery exists to prevent, so the predicate names itself.
    """
    return container.get("status") == "missing"


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
        if _is_confirmed_gone(docker.get_container_status(name)):
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
            # Anything short of confirmed-gone counts as present, so an
            # `unknown` sends us down the reap path and the deploy fails
            # loudly rather than racing a container that may still hold the
            # GPU. The asymmetry with `_is_confirmed_gone` is the point.
            present = not _is_confirmed_gone(docker.get_container_status(target))
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


#: How much of a dying container's output to keep on the record.
FINAL_LOG_LINES = 500


def _final_logs(docker: Any, name: str) -> str:
    """The container's output, read while there is still a container.

    Teardown removes the container, and ``docker logs`` on a removed container
    is not "empty", it is ``No such container``. So a deployment that failed
    lost the only explanation of *why* the moment it was cleaned up, and the
    operator was left with a stopped card and a log pane repeating "Container
    ... not found" — which says nothing about the failure and is the one
    question they came to the page with.
    """
    try:
        text = docker.get_logs(name, tail=FINAL_LOG_LINES)
    except Exception as exc:  # pragma: no cover - best effort by definition
        logger.debug("could not read the final logs of %s: %s", name, exc)
        return ""
    text = str(text or "")
    # The service reports a missing container in-band; that is not a log.
    if text.startswith(f"Container '{name}' not found"):
        return ""
    return text


def _teardown_entry(docker: Any, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Stop one rank. Returns an orphan record when it cannot be confirmed.

    ``entry`` is updated in place with ``final_logs`` — read before the stop,
    because after it there is nothing left to read.
    """
    name = str(entry.get("container_name") or "")
    if not name:
        return None
    entry["final_logs"] = _final_logs(docker, name)
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


# ── Per-deployment lifecycle lock ────────────────────────────────────────────
#
# Create, stop and delete each mutate the same containers, and a background
# ``_pull_then_start`` can be creating a rank at the very moment a teardown
# walks its record. Without a lock the two interleave: a stop tears down the
# ranks it can see, finds none because the create has not launched them yet,
# marks the record stopped, and *then* the create launches a container for a
# record nobody will ever tear down again — a stranded rank holding the GPU.
#
# One lock per deployment id, so unrelated deployments never wait on each
# other. The guard is held only long enough to fetch-or-create the id's lock;
# the lock itself is re-entrant, because ``delete_deployment`` holds it across
# a ``stop_deployment`` that takes it again on the same thread.
_lifecycle_locks: dict[str, threading.RLock] = {}
_lifecycle_locks_guard = threading.Lock()


def _lifecycle_lock(deployment_id: str) -> threading.RLock:
    """The re-entrant lock serialising this deployment's create/stop/delete."""
    with _lifecycle_locks_guard:
        lock = _lifecycle_locks.get(deployment_id)
        if lock is None:
            lock = threading.RLock()
            _lifecycle_locks[deployment_id] = lock
        return lock


def _is_torn_down(deployment_id: str) -> bool:
    """Whether a stop or delete has already settled this record.

    A delete removes the record outright; a stop leaves it ``stopped``. Either
    is a deliberate teardown, and a starter or watcher that observes it must
    not write over it — a readiness result reported after a stop reads as a
    crash, and a container launched after one is an orphan.
    """
    record = get_deployment(deployment_id)
    return record is None or str(record.get("status")) == "stopped"


def _teardown_requested(deployment_id: str) -> bool:
    """Whether the create path should abort rather than launch anything.

    Two signals cover two moments. The pull-cancel flag is set while a stop
    races an in-flight pull, before the record has changed. Once the stop has
    actually run it is the record that says so — :func:`_is_torn_down`. A
    background start re-reads this at each phase boundary so it never builds a
    container for a record that is on its way out.
    """
    return _pull_cancel_requested(deployment_id) or _is_torn_down(deployment_id)


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


#: How many times one node's pull is attempted before the deploy is failed.
#:
#: A pull crossing a slow link is the one step of a deploy that fails for
#: reasons that have nothing to do with the deployment — a registry that drops
#: a connection, a Wi-Fi link that goes away for a minute. Docker keeps the
#: layers it already finished, so attempt two resumes rather than restarts,
#: which is what makes retrying cheap enough to be the default. Three is a
#: bound, not a hope: past it the failure is real and the operator is told.
PULL_ATTEMPTS = 3

#: Seconds to wait before each retry, indexed by the attempt just lost.
#:
#: Short, because the common cause is one dropped connection rather than an
#: outage; long enough that three attempts do not all land inside the same
#: half-minute network hiccup.
PULL_RETRY_BACKOFF_SECONDS = (10.0, 30.0)


def _bytes_transferred(snapshot: dict[str, Any]) -> str:
    """How far the pull got, from the last progress snapshot we saw.

    An operator reading a failed pull asks exactly this: a pull that died at
    2.9 GB of 26 GB is a link that gave out, and one that died at zero is a
    registry that never answered. They need different actions, so the message
    has to tell them apart.
    """
    done = int(snapshot.get("bytes_done") or 0)
    total = int(snapshot.get("bytes_total") or 0)
    if not done and not total:
        return "no bytes transferred"
    if not total:
        return f"{done / 1e9:.2f} GB transferred"
    return (
        f"{done / 1e9:.2f} GB of {total / 1e9:.2f} GB "
        f"({snapshot.get('percent', 0)}%) transferred"
    )


def _pull_failure_message(
    ref: str, where: str, attempts: int, progress: dict[str, Any], exc: Exception
) -> str:
    """Why the pull failed, in the terms the operator can act on."""
    got = _bytes_transferred(progress)
    tried = "1 attempt" if attempts == 1 else f"{attempts} attempts"
    if isinstance(exc, PullStalled):
        # The node's watchdog fired: no bytes *at all* for the window, which
        # is a dead connection rather than a slow one. Say which, because
        # "the pull failed" about a link that had been moving for twenty
        # minutes reads as a transfer that was too slow, and it was not.
        return (
            f"could not pull image {ref} on {where} after {tried}: the registry "
            f"went silent — {exc} — with {got}"
        )
    return f"could not pull image {ref} on {where} after {tried}: {exc} — {got}"


def _pull_with_retries(
    docker: Any,
    dep_id: str,
    ref: str,
    where: str,
    progress: Callable[[dict[str, Any]], None],
    last: dict[str, Any],
) -> Any:
    """Pull ``ref``, retrying a failure up to :data:`PULL_ATTEMPTS` times.

    Returns the pull outcome, or raises :class:`NativeRuntimeError` once the
    attempts are spent — never both, and never neither.
    """
    for attempt in range(1, PULL_ATTEMPTS + 1):
        try:
            return docker.pull_image(
                ref, progress, cancel=lambda: _pull_cancel_requested(dep_id)
            )
        except PullCancelled:
            publish_event(
                EventType.IMAGE_PULL_CANCELLED,
                dep_id,
                f"pull of {ref} cancelled",
                {"image_ref": ref, "node": where},
            )
            raise
        except Exception as exc:
            # A teardown that arrived mid-pull is not a failure to retry: the
            # record is already on its way out, and attempt two would pull an
            # image for a deployment nobody wants.
            if _teardown_requested(dep_id):
                raise PullCancelled(f"pull of {ref} cancelled") from exc
            if attempt < PULL_ATTEMPTS:
                delay = PULL_RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(PULL_RETRY_BACKOFF_SECONDS) - 1)
                ]
                message = (
                    f"pull of {ref} on {where} failed ({exc}); retrying in "
                    f"{delay:g}s — attempt {attempt + 1} of {PULL_ATTEMPTS}"
                )
                logger.warning(message)
                publish_event(
                    EventType.IMAGE_PULL_PROGRESS,
                    dep_id,
                    message,
                    {
                        **last,
                        "image_ref": ref,
                        "node": where,
                        "attempt": attempt + 1,
                        "attempts": PULL_ATTEMPTS,
                    },
                )
                time.sleep(delay)
                continue
            message = _pull_failure_message(ref, where, attempt, last, exc)
            publish_event(
                EventType.IMAGE_PULL_FAILED,
                dep_id,
                message,
                {**last, "image_ref": ref, "node": where},
            )
            raise NativeRuntimeError(message) from exc
    # Unreachable: the loop either returns or raises on every path.
    raise NativeRuntimeError(f"could not pull image {ref} on {where}")


def _pull_image_if_missing(docker: Any, plan_obj: DeployPlan, node: str = "") -> bool:
    """Pull the plan's image before the container is created, with progress.

    ``containers.run`` pulls implicitly and silently, so a deploy against an
    image the host lacks used to sit with no output for the tens of minutes a
    26 GB engine image takes. Doing it here makes the wait visible: the record
    goes to ``pulling`` and aggregated progress events flow over SSE.

    Returns True when a pull actually ran. Raises :class:`NativeRuntimeError`
    when every attempt fails — naming the node, how far the last one got and,
    for a stall, that the registry went silent — or :class:`PullCancelled`
    when a teardown stopped it. What it must never do is return without
    either an image or a raised failure: the record sits in ``pulling``, and
    a caller that is told neither leaves it there with no pull behind it.
    """
    dep_id = plan_obj.deployment_id
    ref = plan_obj.container.image
    where = node or getattr(docker, "label", "") or "this machine"
    try:
        if docker.image_exists(ref):
            return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("image presence check failed for %s: %s", ref, exc)

    _update_record(dep_id, status="pulling")
    publish_event(
        EventType.IMAGE_PULL_STARTED,
        dep_id,
        f"pulling {ref} on {where}",
        {"image_ref": ref, "percent": 0.0, "node": where},
    )

    last: dict[str, Any] = {}

    def _progress(snapshot: dict[str, Any]) -> None:
        last.update(snapshot)
        publish_event(
            EventType.IMAGE_PULL_PROGRESS,
            dep_id,
            f"pulling {ref}: {snapshot.get('percent', 0)}%",
            {"image_ref": ref, "node": where, **snapshot},
        )

    _register_pull(dep_id)
    try:
        result = _pull_with_retries(docker, dep_id, ref, where, _progress, last)
    finally:
        _unregister_pull(dep_id)

    publish_event(
        EventType.IMAGE_PULL_COMPLETED,
        dep_id,
        f"pulled {ref}",
        {
            "image_ref": ref,
            "node": where,
            **(result if isinstance(result, dict) else {}),
        },
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
    with tools.deployment_records.transaction():
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
    # them owned by root and every later write to the HF cache fails — and so
    # do the destinations nested inside the home bind, for the same reason.
    if spec.mounts:
        try:
            unmade = docker.ensure_directories(_bind_sources_to_create(spec.mounts))
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
            user=spec.user,
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
    """Exec one rank's rendered script in the container already created for it.

    Raises :class:`NativeRuntimeError` for anything that goes wrong, including
    a node that stopped answering between creating its container and launching
    it. That last case used to arrive here as a ``False`` return from
    ``copy_to_container``, because the transport could not tell "the copy
    failed" from "the node is gone" and had to pick one. It can now, so the
    conversion happens *here* — where the answer is the same either way (the
    gang cannot start) and the rank's container is still recorded as
    outstanding rather than assumed absent.
    """
    try:
        _deploy_script(docker, rank_plan)
    except NativeRuntimeError:
        raise
    except Exception as exc:
        raise NativeRuntimeError(
            f"could not launch {rank_plan.container.name} on "
            f"{rank_plan.node or 'this machine'}: {exc}"
        ) from exc
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

    def _fail(
        message: str,
        orphans: list[dict[str, Any]] | None = None,
        final_logs: dict[str, str] | None = None,
    ) -> dict:
        updated = _record_error(
            dep_id,
            message,
            orphans=orphans or [],
            final_logs=final_logs or {},
        )
        return updated or {
            **record,
            "status": "error",
            "error_message": _error_text(message),
        }

    # Nothing of an earlier attempt may still be alive when this one claims
    # the names and the GPUs.
    try:
        _reap_earlier_generations(services, plan_obj)
    except NativeRuntimeError as exc:
        return _fail(str(exc))
    except Exception as exc:
        # Resolving a node's service can fail outright — it is not registered,
        # its agent is not connected — and that is not a NativeRuntimeError.
        # It used to leave this function without settling the record it had
        # just written, which on the background path is a deployment stuck at
        # its initial status with nothing working on it.
        logger.exception("could not reap earlier generations of %s", dep_id)
        return _fail(f"could not reach the nodes for {dep_id}: {exc}")

    try:
        for address in _pull_targets(plan_obj):
            _pull_image_if_missing(services(address), plan_obj, address)
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
    except Exception as exc:
        # Everything else the pull phase can raise — a node that is not
        # registered, an agent that stops answering, a bug in the resolver —
        # used to escape this function entirely. On the background path that
        # killed the thread and left the record in "pulling" with no pull
        # behind it, forever: no error, no stopped_at, and a Jobs page saying
        # a deploy was still downloading hours after nothing was. A record
        # mid-creation has exactly one owner, and it is this call; it does
        # not get to return without settling it.
        logger.exception("the pull phase of %s failed", dep_id)
        return _fail(f"could not pull image {spec.image}: {exc}")

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
        entries = [_rank_record(r) for r in pending]
        orphans = _teardown_entries(services, entries)
        # The engine's own last words, kept before the teardown removed the
        # container holding them. This is the failure an operator most needs
        # to read — "no such model", a CUDA error, an OOM — and until now it
        # was destroyed by the cleanup that followed it.
        return _fail(
            f"rank {rank_plan.rank} of {plan_obj.node_count} on "
            f"{rank_plan.node or 'this machine'} failed to {phase}, so the "
            f"whole deployment was torn down: {exc}",
            orphans,
            {
                str(e.get("rank", 0)): e["final_logs"]
                for e in entries
                if e.get("final_logs")
            },
        )

    def _cancelled() -> dict[str, Any]:
        # A stop or delete arrived after the pull, so the pull-cancel hook
        # never fired. Tear down anything this attempt created and settle the
        # record stopped — a deliberate teardown, not a crash. If the record
        # was deleted underneath us, the returned dict is only a value; the
        # store already holds nothing.
        if touched:
            _teardown_entries(services, [_rank_record(r) for r in touched])
        message = f"deploy of {dep_id} cancelled by teardown"
        publish_event(EventType.DEPLOYMENT_STOPPED, dep_id, message)
        return _update_record(dep_id, status="stopped", stopped_at=_now()) or {
            **record,
            "status": "stopped",
        }

    # The create and launch phases run under the deployment's lifecycle lock so
    # a concurrent stop or delete cannot interleave with them: the teardown
    # either runs entirely before this section (and the re-check below catches
    # it) or entirely after (and it tears down a gang that fully exists). The
    # re-checks are what stops a create that has already begun from launching a
    # container for a record a stop settled while we waited for the lock.
    with _lifecycle_lock(dep_id):
        if _teardown_requested(dep_id):
            return _cancelled()

        for rank_plan in plan_obj.teardown_order():
            try:
                _create_rank(services(rank_plan.node), plan_obj, rank_plan, warnings)
            except NativeRuntimeError as exc:
                return _abort(rank_plan, exc, "start")
            touched.append(rank_plan)

        # Every container now exists but none is serving. This is the last
        # point a teardown can be honoured without stranding one, so it is
        # re-checked here as well as before the create.
        if _teardown_requested(dep_id):
            return _cancelled()

        for rank_plan in plan_obj.start_order():
            # Workers first, then the head — and for an engine whose workers
            # are the servers, not merely after them but after they answer.
            if rank_plan.is_head:
                try:
                    _wait_rpc_workers(services, plan_obj)
                except NativeRuntimeError as exc:
                    return _abort(rank_plan, exc, "launch")
            try:
                _launch_rank(services(rank_plan.node), plan_obj, rank_plan)
            except NativeRuntimeError as exc:
                return _abort(rank_plan, exc, "launch")

        # Write "starting" (and, for the fire-and-forget path, "running") while
        # still holding the lock. A stop waiting on the lock then settles the
        # record strictly after these writes and its "stopped" verdict stands —
        # rather than this method releasing the lock, a stop tearing down and
        # marking stopped, and then these writes resurrecting it to "running"
        # over a gang that no longer exists.
        started = _now()
        launched = _update_record(
            dep_id, status="starting", started_at=started, warnings=warnings
        )
        publish_event(
            EventType.DEPLOYMENT_SERVING,
            dep_id,
            f"launch script running in {spec.name}",
            {"command": plan_obj.launch_command},
        )

        if not wait:
            # Still "starting". The script has been exec'd and nothing has
            # answered yet, and writing "running" here is what made the list
            # say running while the detail said starting — over a container
            # whose engine had exited 0.4s in. Readiness is written where it
            # is observed, which on this path is `create_deployment._watch`.
            return launched or record

    # Readiness is awaited without the lock, so a stop can interrupt a starting
    # deployment. Each terminal write then re-checks teardown under the lock: a
    # stop that settled the record while we waited keeps its verdict, because a
    # readiness result reported after a deliberate stop reads as a crash.
    timeout = ready_timeout or config.deploy_ready_timeout_seconds
    try:
        _wait_ready(services, plan_obj, timeout)
    except NativeRuntimeError as exc:
        with _lifecycle_lock(dep_id):
            if _is_torn_down(dep_id):
                return get_deployment(dep_id) or {**record, "status": "stopped"}
            return _fail(str(exc))

    with _lifecycle_lock(dep_id):
        if _is_torn_down(dep_id):
            return get_deployment(dep_id) or {**record, "status": "stopped"}
        publish_event(
            EventType.DEPLOYMENT_READY,
            dep_id,
            f"{plan_obj.recipe_id} is serving on port {plan_obj.port}",
            {"port": plan_obj.port, "readiness_url": plan_obj.readiness_url},
        )
        return (
            _update_record(
                dep_id, status="running", ready_at=_now(), error_message=None
            )
            or record
        )


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
        dep_id = plan_obj.deployment_id
        try:
            _wait_ready(services, plan_obj, config.deploy_ready_timeout_seconds)
        except NativeRuntimeError as exc:
            # A stop or delete may have settled this record while we waited. A
            # readiness timeout after a deliberate teardown is not a crash, and
            # writing "error" over "stopped" — or resurrecting a deleted record
            # — would report one. Leave the teardown's verdict standing.
            if _is_torn_down(dep_id):
                return
            _record_error(dep_id, exc)
            return
        # Readiness is observed here, so it is written here: this thread owns
        # the record from the launch to the first answer, and until it wrote
        # one the row said "running" from the moment the script was exec'd.
        with _lifecycle_lock(dep_id):
            if _is_torn_down(dep_id):
                return
            _update_record(
                dep_id, status="running", ready_at=_now(), error_message=None
            )
            publish_event(
                EventType.DEPLOYMENT_READY,
                dep_id,
                f"{plan_obj.recipe_id} is serving on port {plan_obj.port}",
                {"port": plan_obj.port},
            )

    if _image_missing(services, plan_obj):
        record = persist_planned_record(plan_obj, "pulling")

        def _pull_then_start() -> None:
            dep_id = plan_obj.deployment_id
            try:
                started = start(
                    plan_obj, services=services, wait=False, initial_status="pulling"
                )
            except BaseException as exc:  # noqa: BLE001 — the record outlives us
                # This thread is the only thing that will ever move this record
                # out of "pulling". If it dies here the record stays there with
                # no pull behind it and nothing to notice, which is how a
                # stalled pull became a deployment that downloaded forever.
                logger.exception("the background start of %s failed", dep_id)
                if not _is_torn_down(dep_id):
                    _record_error(
                        dep_id, f"the deploy of {dep_id} failed to start: {exc}"
                    )
                return
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
    if get_deployment(deployment_id) is None:
        return None
    # A deployment still pulling has no container to stop; what has to stop is
    # the download. Ask before taking the lock — the pull runs outside it — so
    # a start racing this stop sees the cancel and aborts rather than launching
    # a container the teardown below would never see.
    cancel_pull(deployment_id)
    # The lock serialises this teardown against a create's launch phase: the
    # two cannot interleave, so we never tear down a half-created gang and then
    # have the create launch the other half behind us.
    with _lifecycle_lock(deployment_id):
        record = get_deployment(deployment_id)
        if record is None:
            return None
        services = services or rank_services(docker)
        entries = rank_entries(record)
        orphans = _teardown_entries(services, entries)
        names = ", ".join(str(e.get("container_name") or "") for e in entries)
        publish_event(EventType.DEPLOYMENT_STOPPED, deployment_id, f"stopped {names}")
        return _update_record(
            deployment_id,
            status="stopped",
            stopped_at=_now(),
            orphans=orphans,
            final_logs={
                str(e.get("rank", 0)): e["final_logs"]
                for e in entries
                if e.get("final_logs")
            },
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
    if get_deployment(deployment_id) is None:
        return False
    # Set the pull-cancel flag before the lock, for the same reason stop does:
    # the pull runs outside the lock, so a racing start must be able to see the
    # cancel and abort rather than launch a container for a record we drop.
    cancel_pull(deployment_id)
    # The whole stop-then-drop runs under the lifecycle lock — re-entrant, so
    # the nested stop takes it again on this thread — so a create cannot slip a
    # freshly launched container in between the teardown and the record drop.
    with _lifecycle_lock(deployment_id):
        if get_deployment(deployment_id) is None:
            return False
        # Always tear the containers down, whatever the record says: a
        # deployment that errored during readiness still has a container
        # running.
        stopped = stop_deployment(deployment_id, docker=docker, services=services)
        if stopped is not None and stopped.get("orphans"):
            return False
        with tools.deployment_records.transaction():
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
    if str(record.get("status") or "") in ("pulling", "pending"):
        # There is no container yet, and that is the plan. "Container ... not
        # found" answers a question nobody asked and reads as a dead deploy;
        # what the operator wants to know is that the image is still coming.
        image = str(record.get("image_ref") or "the image")
        return (
            f"No container yet: {image} is still being pulled. "
            "Logs start when the container does."
        )
    services = services or rank_services(docker)
    entry = next(
        (e for e in rank_entries(record) if int(e.get("rank", 0)) == rank), None
    )
    if entry is None:
        return f"Deployment has no rank {rank}"
    name = str(entry.get("container_name") or container_name_for(deployment_id))
    kept = str((record.get("final_logs") or {}).get(str(rank)) or "")
    try:
        service = services(str(entry.get("node") or ""))
    except Exception as exc:
        if kept:
            return kept
        return f"Failed to reach {entry.get('node') or 'this machine'}: {exc}"
    live = logs_for_container(service, name, lines)
    # A removed container answers "not found", which is not what the operator
    # asked. What they asked is what this deployment said before it stopped,
    # and that was kept at teardown precisely because the container would not
    # survive to be asked.
    if kept and (not live or live.startswith(f"Container '{name}' not found")):
        return kept
    return live or "(empty log)"


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


def _gather_rank_statuses(
    services: Callable[[str], Any], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every rank's container state, asked for at the same time.

    Serially, a rank on a silent node cost the whole request its own timeout,
    and the ranks behind it waited their turn: four ranks with one silent node
    took 10.14 s where four healthy ones took 0.13 s, measured
    (``docs/rank-state-transport.md`` §2.2). ``GET /api/deployments/{id}`` is
    a sync route, so that was an AnyIO threadpool worker held for ten seconds,
    on every poll, for as long as the node stayed down.

    Concurrently, the cost is the slowest rank rather than the sum, and with
    :data:`~spark_pulse.tools.node_service.STATUS_PROBE_TIMEOUT` bounding each
    probe the slowest rank is bounded too. Nothing here raises:
    :func:`_rank_status` turns a failure into ``unknown``, so a dead node
    marks its own rank and the endpoint still answers.

    Order is the rank order the record gives, because ``Executor.map`` yields
    in submission order and rank zero is what ``status`` reports as *the*
    container.
    """
    if len(entries) < 2:
        return [_rank_status(services, entry) for entry in entries]
    workers = min(len(entries), RANK_STATUS_MAX_WORKERS)
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="rank-status"
    ) as pool:
        return list(pool.map(lambda entry: _rank_status(services, entry), entries))


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
    ranks = _gather_rank_statuses(services, rank_entries(record))
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
    # A record mid-creation has no container yet *by design*: the image is
    # still being pulled, or the gang is about to be created under the
    # lifecycle lock. Reading "no running container" as "stopped" here made
    # every deploy that needed a pull report itself stopped for the whole
    # pull. The record's own status is the truth until a container exists.
    if record.get("status") in ("pulling", "pending"):
        return str(record["status"])
    if record.get("status") == "starting" and container.get("status") == "missing":
        return "starting"
    if not container.get("running"):
        return "stopped"
    # A running container is not a running engine. The container runs a
    # keepalive and the engine is an exec inside it, so "the container is up"
    # answered *running* over a `llama-server` that had exited 0.4s in — while
    # the list, which reads the row, still said starting. The verdict is
    # readiness: observed now, or observed once and written down.
    return "running" if (ready or _ever_ready(record)) else "starting"


def _ever_ready(record: dict[str, Any]) -> bool:
    """Whether this deployment was ever *observed* serving.

    ``ready_at`` is written where readiness is seen — `start`'s terminal write
    and `create_deployment._watch` — so it is evidence rather than inference.
    A record adopted from a container this control plane did not start carries
    none and never will: nothing watched it come up, its own row is all there
    is, and the alternative is a list and a detail that disagree about it
    forever.
    """
    return bool(record.get("ready_at") or record.get("reconciled"))


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
        # "pulling" and a not-yet-launched "starting" are records mid-creation:
        # their containers legitimately do not exist yet, and `start()` owns
        # that transition under the lifecycle lock. Marking them "stopped" on
        # the absence of a container that has not been created would race the
        # creator into aborting itself — so this reconcile only judges records
        # that should already have containers.
        #
        # `started_at` is what tells the two apart, and it is written in the
        # same locked block as "starting", strictly after every rank has been
        # created and launched. A record that stays "starting" now stays there
        # for the whole readiness window — up to `deploy_ready_timeout_seconds`
        # — because "running" is only written where readiness is observed, and
        # a control plane that restarts inside that window takes the watcher
        # with it. Without this, that record would say *starting* forever.
        if record.get("status") in ("stopped", "error", "pulling"):
            continue
        if record.get("status") == "starting" and not record.get("started_at"):
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
            # Nothing watched this one start, so there is nothing to record;
            # `_ever_ready` reads `reconciled` for exactly this case.
            "ready_at": None,
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

    It also *finishes the teardown* rather than only watching for it. An
    orphan is a rank that was asked to stop and never confirmed gone, and for
    as long as ``RemoteNodeService.stop_container`` stopped a peer's container
    without removing it, every multi-node teardown produced one: the container
    sat in ``exited`` answering ``docker inspect``, so ``missing`` never came
    and the record held its ports for good. Removal is fixed now, but the
    records that leak was already writing are on disk, and nothing that only
    *looks* at a stopped container will ever clear them. So when the node
    answers and the container is still there, the stop is asked for again —
    idempotent on a container that is already stopped, and the one thing that
    turns an inherited orphan into a freed port range. That is the migration:
    the first sweep after this change clears them, with no separate step.
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
                service = services(str(orphan.get("node") or ""))
                status = service.get_container_status(name)
                gone = _is_confirmed_gone(status)
                if not gone and status.get("status") != "unknown":
                    # The node answered and the container is still there:
                    # retry the removal rather than wait for someone else.
                    service.stop_container(name)
                    gone = _is_confirmed_gone(service.get_container_status(name))
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
