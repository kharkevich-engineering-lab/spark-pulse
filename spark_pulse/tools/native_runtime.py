"""Native solo deployment runtime — Docker driven from Python.

This is the phase-1 replacement for forking upstream's ``run-recipe.sh``. It
reproduces the upstream lifecycle described in the native-runtime plan §1.4:

1. :func:`plan` resolves everything up front — engine, image, model, mods,
   port, container profile and the rendered per-rank launch script — and
   returns a serialisable :class:`DeployPlan`. Nothing is started, so the same
   call backs the UI's "Preview" button and ``POST /api/deployments/plan``.
2. :func:`start` runs an *idle* container (``sleep infinity``), applies the
   recipe's mods with ``docker exec``, copies the rendered script to
   ``/workspace/exec-script.sh`` and execs it detached with output redirected
   to PID 1's stdout so ``docker logs`` carries the serve output, then waits
   for the engine's readiness endpoint.

Only solo (single node) deployments are supported here; the cluster path stays
on ``tools.cluster``. Everything switchable goes through ``spark_pulse.tools``
so simulation mode swaps the container service; ``spark_pulse.engines`` is
imported directly because rendering is pure.
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
from typing import Any

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
from spark_pulse.tools.docker import ContainerMetadata, PullCancelled
from spark_pulse.tools.events import DeploymentEvent, EventType
from spark_pulse.tools.labels import DEPLOYMENT_LABEL, MODE_LABEL

logger = logging.getLogger(__name__)

RUNTIME_NAME = "native"
SCRIPT_PATH = "/workspace/exec-script.sh"
MODS_DIR = "/workspace/mods"
CONTAINER_PREFIX = "spark-pulse-"
CONTAINER_HOME = "/root"
HF_CACHE_IN_CONTAINER = "/root/.cache/huggingface"


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
    image_present: bool = True
    image_size_bytes: int | None = None
    workdir: str = ""
    warnings: list[str] = field(default_factory=list)
    runtime: str = RUNTIME_NAME
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["container"] = self.container.to_dict()
        return data


# ── Helpers ──────────────────────────────────────────────────────────────────


def container_name_for(deployment_id: str) -> str:
    return f"{CONTAINER_PREFIX}{deployment_id}"


def _docker_service() -> Any:
    """The container service for this process (real or mock)."""
    return tools.docker._get_service()


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
    ports: set[int] = set()
    for record in _load_records():
        if record.get("status") in ("stopped", "error"):
            continue
        port = record.get("port")
        if isinstance(port, int):
            ports.add(port)
    return ports


# ── Persistence (shared deployments.json) ────────────────────────────────────


def _load_records() -> list[dict[str, Any]]:
    """Load the shared deployment records.

    The native runtime shares ``deployments.json`` with the container runtime,
    so it inherits that module's crash-safe write and its refusal to read an
    unreadable state file as an empty one: ``StateFileError`` propagates to the
    caller rather than degrading into ``[]``.
    """
    return tools.deployments._load()


def _save_records(records: list[dict[str, Any]]) -> None:
    """Persist the shared deployment records.

    Delegates to ``tools.deployments._save``, which writes through
    ``atomic_json.write_json_atomic`` — temp file, fsync, replace, dir fsync.
    """
    tools.deployments._save(records)


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
    node: NodeInfo | None,
) -> dict[str, str]:
    env = dict(
        engine_obj.base_env(
            node_ip=node.ip if node else "",
            eth_if=node.eth_if if node else "",
            ib_if=node.ib_if if node else "",
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

    resolved_model = _resolve_model(recipe, model, allow_missing_model, warnings)

    mods = engine_obj.block_mods(recipe)
    if mods and not engine_obj.supports_mods():
        raise NativeRuntimeError(
            f"recipe '{recipe_id}' needs mods ({', '.join(mods)}) but engine "
            f"'{engine_name}' does not support them"
        )

    node_list = [] if solo else [str(n) for n in (nodes or [])]
    topology = Topology(nodes=[NodeInfo(host=n, ip=n) for n in node_list])

    # Only the caller's own overrides are handed to the engine: the engine
    # merges the recipe's defaults itself, and it distinguishes "the user asked
    # for this" from "the recipe defaults to this" (solo forces tensor_parallel
    # to 1 unless explicitly overridden, exactly as upstream does).
    overrides = {k: v for k, v in (params or {}).items() if v is not None}
    defaults = recipe.get("defaults") or {}
    port = overrides.get("port") or defaults.get("port")
    if port in (None, "", 0):
        port = allocate_port(_ports_in_use())
        overrides["port"] = port
    port = int(port)
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

    dep_id = deployment_id or uuid.uuid4().hex[:12]
    profile = _container_profile(engine_obj)
    env = _build_env(engine_obj, recipe, topology.node(0))
    mounts, cache_mounts = _build_mounts(engine_obj)
    network_host = bool(profile.get("network_host", False))
    # In solo the API port is published only when the container is not on the
    # host network; on host networking the engine already binds it directly.
    port_mappings = [] if network_host else [f"{port}:{port}"]

    metadata = ContainerMetadata(
        deployment=dep_id,
        recipe=str(recipe.get("id") or recipe_id),
        image=image_ref,
        mode="solo" if topology.is_solo else "cluster",
        created_at=_now(),
        memory_limit_gb=config.docker_memory_limit_gb,
        shm_size_gb=float(profile.get("shm_size_gb") or config.docker_shm_size_gb),
        privileged=bool(profile.get("privileged", True)),
    )

    container = ContainerSpec(
        image=image_ref,
        name=container_name_for(dep_id),
        command=str(profile.get("keepalive") or "sleep infinity"),
        env=env,
        labels=metadata.to_labels(),
        mounts=mounts,
        privileged=bool(profile.get("privileged", True)),
        ipc_host=bool(profile.get("ipc_host", False)),
        network_host=network_host,
        shm_size_gb=float(profile.get("shm_size_gb") or config.docker_shm_size_gb),
        devices=[str(d) for d in (profile.get("devices") or [])],
        cap_add=[str(c) for c in (profile.get("cap_add") or [])],
        ulimits={str(k): str(v) for k, v in (profile.get("ulimits") or {}).items()},
        memory_limit_gb=config.docker_memory_limit_gb,
        pids_limit=config.docker_pids_limit,
        nofile_limit=config.docker_nofile_limit,
        port_mappings=port_mappings,
        entrypoint_clear=not config.docker_keep_entrypoint,
    )

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
        rendezvous_port=engine_obj.rendezvous_port(),
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
        "launch_command": plan_obj.launch_command,
        "log_path": None,
        # Native additions.
        "runtime": RUNTIME_NAME,
        "engine": plan_obj.engine,
        "variant": plan_obj.variant,
        "image_ref": plan_obj.image_ref,
        "model": plan_obj.model,
        "container_name": plan_obj.container.name,
        "image_present": plan_obj.image_present,
        "node_count": plan_obj.node_count,
        "mods": plan_obj.mods,
        "readiness_url": plan_obj.readiness_url,
    }


# ── Starting ─────────────────────────────────────────────────────────────────


def _resolve_mod_dir(mod: str) -> Path:
    """Locate a recipe's mod on disk.

    Recipes name mods the way upstream does, relative to the checkout root
    (``mods/fix-x``), but a bare name is accepted too.
    """
    root = Path(config.spark_vllm_path)
    candidates = [root / mod]
    if not mod.startswith("mods/"):
        candidates.append(root / "mods" / mod)
    for candidate in candidates:
        if (candidate / "run.sh").is_file():
            return candidate
    raise NativeRuntimeError(
        f"mod '{mod}' has no run.sh under {root}; looked in "
        + ", ".join(str(c) for c in candidates)
    )


def _apply_mods(docker: Any, plan_obj: DeployPlan, warnings: list[str]) -> list[str]:
    """Copy each mod into the container and run its ``run.sh``, upstream style.

    ``WORKSPACE_DIR`` is the image's working directory, as upstream sets it
    from ``$PWD``: mods drop files there and recipes reference them by bare
    name (``--chat-template unsloth.jinja``).
    """
    applied: list[str] = []
    workdir = plan_obj.workdir or "/workspace"
    for mod in plan_obj.mods:
        mod_dir = _resolve_mod_dir(mod)
        name = mod_dir.name
        remote = f"{MODS_DIR}/{name}"
        docker.exec_in_container(plan_obj.container.name, ["mkdir", "-p", remote])
        for path in sorted(mod_dir.iterdir()):
            # docker cp takes files and directories alike.
            docker.copy_to_container(
                plan_obj.container.name, str(path), f"{remote}/{path.name}"
            )
        result = docker.exec_in_container(
            plan_obj.container.name,
            ["bash", "-lc", f"cd {remote} && WORKSPACE_DIR={workdir} bash run.sh"],
        )
        if not result.ok:
            raise NativeRuntimeError(
                f"mod '{mod}' failed (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )
        applied.append(mod)
    return applied


def _deploy_script(docker: Any, plan_obj: DeployPlan) -> None:
    """Copy the rendered script in and exec it detached, logging to PID 1."""
    script = plan_obj.ranks[0]["script"]
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", prefix="spark-pulse-", delete=False
    ) as handle:
        handle.write(script)
        local_path = handle.name
    try:
        if not docker.copy_to_container(
            plan_obj.container.name, local_path, SCRIPT_PATH
        ):
            raise NativeRuntimeError(
                f"could not copy the launch script into {plan_obj.container.name}"
            )
    finally:
        try:
            os.unlink(local_path)
        except OSError:  # pragma: no cover - defensive
            pass

    docker.exec_in_container(plan_obj.container.name, ["chmod", "+x", SCRIPT_PATH])
    docker.exec_in_container(
        plan_obj.container.name,
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
    docker: Any,
    plan_obj: DeployPlan,
    timeout: int,
    interval: float = 2.0,
) -> None:
    """Poll readiness, failing fast when the container exits first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = docker.get_container_status(plan_obj.container.name)
        if not status.get("running"):
            logs = logs_for_container(docker, plan_obj.container.name, 50)
            raise NativeRuntimeError(
                f"container {plan_obj.container.name} exited before the engine "
                f"became ready ({status.get('status')}). Last log lines:\n{logs}"
            )
        if probe_ready(plan_obj.readiness_url):
            return
        time.sleep(interval)
    raise NativeRuntimeError(
        f"engine did not become ready within {timeout}s at " f"{plan_obj.readiness_url}"
    )


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


def _image_missing(docker: Any, plan_obj: DeployPlan) -> bool:
    """Whether this deploy has to pull before it can start anything.

    A daemon that will not answer is not evidence the image is absent, so an
    unreachable check keeps the deploy on the inline path where the failure is
    reported straight back to the caller.
    """
    try:
        return not docker.image_exists(plan_obj.container.image)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("image presence check failed: %s", exc)
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


def start(
    plan_obj: DeployPlan,
    docker: Any | None = None,
    wait: bool = True,
    ready_timeout: int | None = None,
    initial_status: str = "starting",
) -> dict[str, Any]:
    """Run the plan: idle container -> mods -> exec script -> readiness."""
    if not plan_obj.solo:
        raise NativeRuntimeError(
            "the native runtime cannot start a cluster deployment yet; "
            "set runtime: upstream for multi-node recipes"
        )

    docker = docker or _docker_service()
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
        },
    )

    def _fail(message: str) -> dict[str, Any]:
        publish_event(EventType.DEPLOYMENT_ERROR, dep_id, message)
        updated = _update_record(
            dep_id, status="error", error_message=message, stopped_at=_now()
        )
        return updated or {**record, "status": "error", "error_message": message}

    try:
        _pull_image_if_missing(docker, plan_obj)
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

    metadata = ContainerMetadata.from_labels(spec.labels)
    try:
        docker.run_container(
            image=spec.image,
            name=spec.name,
            env_vars=spec.env,
            metadata=metadata,
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
        return _fail(f"could not start container {spec.name}: {exc}")

    publish_event(
        EventType.DEPLOYMENT_CONTAINER_STARTED,
        dep_id,
        f"container {spec.name} started from {spec.image}",
        {"container_name": spec.name, "image_ref": spec.image},
    )

    try:
        applied = _apply_mods(docker, plan_obj, warnings)
    except NativeRuntimeError as exc:
        docker.stop_container(spec.name)
        return _fail(str(exc))
    if plan_obj.mods:
        publish_event(
            EventType.DEPLOYMENT_MODS_APPLIED,
            dep_id,
            f"applied {len(applied)}/{len(plan_obj.mods)} mod(s)",
            {"mods": applied, "warnings": warnings},
        )

    try:
        _deploy_script(docker, plan_obj)
    except NativeRuntimeError as exc:
        docker.stop_container(spec.name)
        return _fail(str(exc))

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
        _wait_ready(docker, plan_obj, timeout)
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

    docker = _docker_service()

    def _watch() -> None:
        try:
            _wait_ready(docker, plan_obj, config.deploy_ready_timeout_seconds)
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

    if _image_missing(docker, plan_obj):
        record = persist_planned_record(plan_obj, "pulling")

        def _pull_then_start() -> None:
            started = start(
                plan_obj, docker=docker, wait=False, initial_status="pulling"
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

    record = start(plan_obj, docker=docker, wait=False)
    if record.get("status") == "error":
        return record

    threading.Thread(
        target=_watch, name=f"native-ready-{plan_obj.deployment_id}", daemon=True
    ).start()
    return record


# ── Lifecycle ────────────────────────────────────────────────────────────────


def stop_deployment(
    deployment_id: str, docker: Any | None = None
) -> dict[str, Any] | None:
    """Stop the container and mark the record stopped."""
    record = get_deployment(deployment_id)
    if record is None:
        return None
    # A deployment still pulling has no container to stop; what has to stop is
    # the download. Ask first, then fall through — the pull thread settles the
    # record itself once it notices.
    cancel_pull(deployment_id)
    docker = docker or _docker_service()
    name = record.get("container_name") or container_name_for(deployment_id)
    try:
        docker.stop_container(name)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Stopping %s failed: %s", name, exc)
    publish_event(EventType.DEPLOYMENT_STOPPED, deployment_id, f"stopped {name}")
    return _update_record(deployment_id, status="stopped", stopped_at=_now())


def delete_deployment(deployment_id: str, docker: Any | None = None) -> bool:
    """Stop (if needed) and drop the record."""
    record = get_deployment(deployment_id)
    if record is None:
        return False
    # Always tear the container down, whatever the record says: a deployment
    # that errored during readiness still has a container running.
    stop_deployment(deployment_id, docker=docker)
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


def get_logs(deployment_id: str, lines: int = 200, docker: Any | None = None) -> str:
    """``docker logs`` for the deployment's container."""
    record = get_deployment(deployment_id)
    if record is None:
        return "Deployment not found"
    docker = docker or _docker_service()
    name = record.get("container_name") or container_name_for(deployment_id)
    return logs_for_container(docker, name, lines) or "(empty log)"


def status(deployment_id: str, docker: Any | None = None) -> dict[str, Any] | None:
    """Live status: container state plus a readiness probe."""
    record = get_deployment(deployment_id)
    if record is None:
        return None
    docker = docker or _docker_service()
    name = record.get("container_name") or container_name_for(deployment_id)
    container = docker.get_container_status(name)
    port = record.get("port")
    url = record.get("readiness_url") or (f"http://127.0.0.1:{port}/v1/models")
    ready = bool(container.get("running")) and probe_ready(url)
    return {
        **record,
        "container": container,
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
    whose container is gone is marked stopped.
    """
    records = _load_records()
    native = [r for r in records if r.get("runtime") == RUNTIME_NAME]
    try:
        docker = docker or _docker_service()
        containers = docker.list_managed_containers({MODE_LABEL: "solo"})
    except Exception as exc:
        logger.debug("Native reconciliation skipped: %s", exc)
        return native

    by_name = {c.name: c for c in containers}
    known_ids = {r.get("id") for r in native}
    changed = False

    for record in native:
        name = record.get("container_name") or container_name_for(str(record.get("id")))
        container = by_name.get(name)
        if record.get("status") in ("stopped", "error"):
            continue
        if container is None or container.status not in ("running", "created"):
            record["status"] = "stopped"
            record.setdefault("stopped_at", _now())
            changed = True

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
            "node_count": 1,
            "mods": [],
            "reconciled": True,
        }
        native.append(adopted)
        records.append(adopted)
        changed = True

    if changed:
        _save_records(records)
    return native
