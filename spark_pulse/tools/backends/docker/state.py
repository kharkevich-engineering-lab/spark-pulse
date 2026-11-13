"""The Docker backend's shared state: constants, the plan model, records and events.

Moved out of ``tools/native_runtime.py`` (#163). Everything the other
submodules share lives here, and they reach it as ``state.<name>`` so that a
patch of one name here applies to every caller.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from spark_pulse import tools
from spark_pulse.tools.docker import (
    AGENT_USER,
)
from spark_pulse.tools.events import DeploymentEvent, EventType

# What a run is, apart from how Docker runs it (#161). The helpers are
# re-exported so callers and tests that reach them here keep working.
# Capacity validation is pure arithmetic with no side effect to simulate, so
# it is imported directly, like ``spark_pulse.engines``.
# What a plan is, apart from how Docker runs it: credentials kept out of what
# a browser reads, the readiness question, and which nodes, ports and memory a
# run occupies. Re-exported under their old names so every caller and every
# test that reaches them here keeps working.
from spark_pulse.tools.redaction import (
    _redact_rank,
    redact_env,
    redact_script,
)

logger = logging.getLogger(__name__)

RUNTIME_NAME = "native"
#: The run backend this module is (``tools.backends.DockerBackend.name``),
#: written onto every record it makes. ``runtime`` above is the older note and
#: stays as it is.
BACKEND_NAME = "docker"
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

#: Where the model an engine serves came from, on a plan and on every rank
#: that loads one. Only engines that would otherwise fetch their own copy
#: carry either value — see :meth:`Engine.resolves_model_file`; the rest carry
#: neither, because "the engine read the cache" is not a choice anybody made.
MODEL_SOURCE_CACHE = "hf-cache"
MODEL_SOURCE_ENGINE = "engine-download"

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
    """Record a deployment event and emit it on the shared broadcaster.

    Recorded *first*, and on the calling thread: the broadcaster reaches
    whoever happens to be listening, and with nobody listening there is simply
    nothing to deliver — which is how a run that was deployed, pulled, started
    and benchmarked came to show an empty event panel an hour later. The write
    is what makes the timeline exist; the broadcast is what makes it live.

    The write can never fail the publish: ``event_log.record`` swallows its own
    errors, and the broadcast below happens whatever it returned.
    """
    from spark_pulse.sse import _get_event_broadcaster
    from spark_pulse.tools import event_log

    event = DeploymentEvent(
        event_type=event_type,
        resource=deployment_id,
        resource_type="deployment",
        message=message or event_type.value,
        metadata=metadata or {},
    )
    event_log.record(event)
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
        """The serialised spec, credentials redacted (see :data:`REDACTED`).

        Nothing launches from this dict — the deploy path holds the object —
        so the real values never need to leave the process.
        """
        data = asdict(self)
        data["env"] = redact_env(self.env)
        return data


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
    #: :data:`MODEL_SOURCE_CACHE` when this rank was handed a file out of the
    #: mounted Hugging Face cache, :data:`MODEL_SOURCE_ENGINE` when the engine
    #: will fetch its own copy, and empty for every rank that does not load a
    #: model — an RPC worker — and every engine that is handed a model id.
    model_source: str = ""
    #: The file that rank loads, as the container sees it. Set only alongside
    #: :data:`MODEL_SOURCE_CACHE`.
    model_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["container"] = self.container.to_dict()
        data["script"] = redact_script(self.script)
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
    #: The head rank's :attr:`RankPlan.model_source` and
    #: :attr:`RankPlan.model_path`, hoisted so the preview can say where the
    #: bytes come from without walking the ranks.
    model_source: str = ""
    model_path: str = ""
    #: Which files of the model's repository this run needs
    #: (:meth:`Engine.model_patterns`) — what a download made on its behalf
    #: is narrowed to. Empty is the whole repository.
    model_patterns: list[str] = field(default_factory=list)
    #: The model the readiness answer has to name, where the readiness path
    #: *is* the engine's model listing (vLLM's ``/v1/models``). A 200 there
    #: says only that *a* server holds the port; this is what makes it ours.
    #: Empty for an engine whose readiness is ``/health``, which names nothing.
    served_model: str = ""
    #: The fraction of each node's memory this run claims, where its engine
    #: takes one (vLLM's ``--gpu-memory-utilization``, SGLang's
    #: ``--mem-fraction-static``) — read from the rendered command, since that
    #: is what the engine will be handed. ``None`` for an engine that
    #: allocates as it goes and so claims no fraction at all.
    gpu_memory_utilization: float | None = None
    #: One entry per node the run occupies: its total memory, the other runs
    #: on it and what each claims, what is left and the largest fraction that
    #: fits (:class:`~spark_pulse.tools.memory_budget.NodeBudget`). The
    #: pre-flight judges it; the preview shows it.
    memory_budget: list[dict[str, Any]] = field(default_factory=list)
    #: What the recipe serves (``recipe_schema.SERVES``), copied onto the
    #: record so a run keeps answering for what it was started as after its
    #: recipe is edited or uninstalled — the benchmark refusal reads it there.
    serves: str = "chat"
    workdir: str = ""
    warnings: list[str] = field(default_factory=list)
    runtime: str = RUNTIME_NAME
    #: The backend that realised this plan, the intent it realised (redacted;
    #: ``RunIntent.to_dict``) and that intent's hash, which two runs of the
    #: same thing share whatever their ids, names and ports turned out to be.
    backend: str = BACKEND_NAME
    intent: dict[str, Any] = field(default_factory=dict)
    intent_hash: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["container"] = self.container.to_dict()
        data["rank_plans"] = [r.to_dict() for r in self.rank_plans]
        data["ranks"] = [_redact_rank(rank) for rank in self.ranks]
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
        # Which backend runs it and what it is (#162). The intent is the
        # redacted one the plan carries; the hash is how a later change can
        # tell "the same run again" from "a different run under one name".
        "backend": plan_obj.backend,
        "intent": plan_obj.intent,
        "intent_hash": plan_obj.intent_hash,
        "engine": plan_obj.engine,
        "variant": plan_obj.variant,
        "image_ref": plan_obj.image_ref,
        "model": plan_obj.model,
        "serves": plan_obj.serves,
        # Rank zero's name, kept as a scalar alias so every existing reader
        # — the health router, the UI, the upstream path — still resolves.
        "container_name": plan_obj.container.name,
        "image_present": plan_obj.image_present,
        "node_count": plan_obj.node_count,
        "mods": plan_obj.mods,
        "readiness_url": plan_obj.readiness_url,
        "served_model": plan_obj.served_model,
        # What the run claims of each node's memory, so the next plan on the
        # same node can budget around it without re-rendering this one's
        # command. ``None`` is an engine that takes no fraction — measured,
        # not assumed, when somebody plans beside it.
        "gpu_memory_utilization": plan_obj.gpu_memory_utilization,
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
