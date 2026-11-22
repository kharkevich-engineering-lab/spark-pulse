"""Realising a run's intent as a Docker plan: images, mounts, the model file and the launch.

Moved out of ``tools/native_runtime.py`` (#163).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.engines import (
    Engine,
    Topology,
    get_registry,
)
from spark_pulse.tools.backends.docker import state
from spark_pulse.tools.docker import (
    ContainerMetadata,
)

# What a run is, apart from how Docker runs it (#161). The helpers are
# re-exported so callers and tests that reach them here keep working.
from spark_pulse.tools.intent import (
    RunIntent,
    resolve_intent,
)

# Capacity validation is pure arithmetic with no side effect to simulate, so
# it is imported directly, like ``spark_pulse.engines``.
# What a plan is, apart from how Docker runs it: credentials kept out of what
# a browser reads, the readiness question, and which nodes, ports and memory a
# run occupies. Re-exported under their old names so every caller and every
# test that reaches them here keeps working.
from spark_pulse.tools.placement import (
    _choose_port,
    _claimed_fraction,
    _memory_budget,
)
from spark_pulse.tools.readiness import (
    _served_model_name,
)

logger = logging.getLogger(__name__)


# ── Planning ─────────────────────────────────────────────────────────────────


def _container_cache_path(host_path: str) -> str:
    """A path inside this machine's Hugging Face cache, as the engine sees it.

    Composed from the host path *relative to* ``HF_HOME`` rather than by
    reassembling the hub layout a second time, so the one thing that has to
    hold — that ``HF_HOME`` is bind-mounted at
    :data:`HF_CACHE_IN_CONTAINER` — is the only thing assumed. A path that is
    not under the cache is left to the generic home rewrite, which is what
    every other mount goes through.
    """
    hf_home = _hf_home()
    if host_path == hf_home or host_path.startswith(hf_home.rstrip("/") + "/"):
        return f"{state.HF_CACHE_IN_CONTAINER}/{os.path.relpath(host_path, hf_home)}"
    return state._container_path(host_path)


def _resolve_model_file(
    engine_obj: Engine,
    recipe: dict[str, Any],
    model_id: str,
    model_present: bool,
    node_address: str,
    services: Callable[[str], Any],
    warnings: list[str],
) -> tuple[str, str]:
    """The file the engine is handed, and where those bytes come from.

    Only for an engine that would otherwise fetch its own copy — llama.cpp,
    whose ``-hf`` downloads the GGUF into ``LLAMA_CACHE`` beside the one the
    control plane put in the Hugging Face cache, replicated to every node and
    then ignored. Every other engine reads the snapshot the catalogue lists,
    so being handed the id *is* being handed the file.

    Two questions, and they are asked of two different things. *Which file* is
    the engine's, and it is pure: :meth:`Engine.choose_model_file` reads the
    recipe's ``--hf-file`` against the names the node answered with. *Is it
    there* is the node's, asked through its own agent (``ListSnapshot``) — the
    node the rank that loads the model runs on, which for an RPC gang is the
    head and only the head, because a worker loads nothing.

    Nothing here is a refusal. A file that cannot be resolved — an unreadable
    node, a snapshot without it, a repository holding several packings and a
    recipe naming none — leaves ``-hf`` exactly as it was and says so as a
    plan warning, because a slow deploy is better than a refused one and the
    operator is the one who can tell which they have.
    """
    if not engine_obj.resolves_model_file() or not model_id:
        return "", ""
    where = node_address or "this node"
    if not model_present:
        # `_resolve_model` has already warned that the model is not in the
        # catalogue. There is no snapshot to look inside, and saying so twice
        # would not add anything.
        return "", state.MODEL_SOURCE_ENGINE

    repo_dir = f"{tools.models.hub_dir()}/{tools.models.repo_dir_name(model_id)}"
    try:
        entry = tools.models.get_model(model_id) or {}
        revision = str(entry.get("revision") or "")
        listing = services(node_address).list_snapshot(repo_dir, revision, False)
    except Exception as exc:  # noqa: BLE001 — a node that cannot be asked
        logger.debug("could not list %s on %s: %s", model_id, where, exc)
        warnings.append(
            f"could not ask {where} whether it holds the model files for "
            f"'{model_id}' ({exc}), so the engine will resolve the model "
            "itself and download its own copy"
        )
        return "", state.MODEL_SOURCE_ENGINE

    # A dangling symlink is not a file: a snapshot copied without its blobs
    # lists every name and resolves none of them.
    files = [f.path for f in listing.files if f.resolved] if listing.present else []
    chosen, reason = engine_obj.choose_model_file(recipe, files)
    if not chosen:
        warnings.append(
            f"the model file for '{model_id}' was not resolved on {where} "
            f"({reason}), so the engine will download its own copy into its "
            "cache, beside the one the control plane already holds"
        )
        return "", state.MODEL_SOURCE_ENGINE
    commit = listing.revision or revision
    return (
        _container_cache_path(f"{repo_dir}/snapshots/{commit}/{chosen}"),
        state.MODEL_SOURCE_CACHE,
    )


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
    env.setdefault("HF_HOME", state.HF_CACHE_IN_CONTAINER)
    # The image's own HOME is root's, and the container is not root's. Set
    # after the recipe's block env, so a recipe that names one still wins.
    env.setdefault("HOME", state.CONTAINER_HOME)
    # Only rank zero carries the hub token. Every other rank loads from the
    # snapshot replication put on its node, so the token has no reason to be
    # there — the rule ``models.worker_env`` already applies to replication —
    # and on a peer it is readable by anyone who can inspect that container.
    if node_rank > 0:
        for key in tools.models.TOKEN_ENV_KEYS:
            env.pop(key, None)
        return env
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
        return state._expand("~/.cache/huggingface")


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
    declared.append(state.ENGINE_HOME_ON_HOST)

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in declared:
        portable = _home_relative(state._expand(raw))
        if portable not in seen:
            seen.add(portable)
            ordered.append(portable)
    return ordered


def _build_mounts(engine_obj: Engine) -> tuple[dict[str, str], list[str]]:
    """Host->container bind mounts for the engine's caches plus ``HF_HOME``."""
    mounts: dict[str, str] = {}
    declared = list(engine_obj.cache_mounts())
    engine_home = state._expand(state.ENGINE_HOME_ON_HOST)
    for raw in engine_cache_dirs(engine_obj):
        host = state._expand(raw)
        # The home is the container's ``$HOME``, not a cache under it, so it
        # is pinned below rather than mapped by path like the others.
        if host != engine_home:
            mounts[host] = state._container_path(host)
    hf_home = _hf_home()
    # HF_HOME wins over any engine-declared cache that targets the same path;
    # docker refuses two binds on one container destination.
    for host, target in list(mounts.items()):
        if target == state.HF_CACHE_IN_CONTAINER and host != hf_home:
            del mounts[host]
    mounts[hf_home] = state.HF_CACHE_IN_CONTAINER
    # The home itself, so ``$HOME`` is writable by the uid the engine runs as
    # rather than a root-owned directory docker invented for the binds beneath
    # it. Nested binds are fine — docker mounts them in path order.
    mounts.setdefault(engine_home, state.CONTAINER_HOME)
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
        if target == state.CONTAINER_HOME:
            home_on_host = host
    wanted = set(mounts)
    if home_on_host:
        prefix = state.CONTAINER_HOME + "/"
        for target in mounts.values():
            if target.startswith(prefix):
                wanted.add(home_on_host + target[len(state.CONTAINER_HOME) :])
    return sorted(wanted)


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
    services: Callable[[str], Any] | None = None,
) -> state.DeployPlan:
    """Resolve a deployment without starting anything.

    Two halves: :func:`~spark_pulse.tools.intent.resolve_intent` says what
    the run is, with nothing about Docker in it, and :func:`realise` says how
    Docker runs it. Raises :class:`NativeRuntimeError` with an explained
    reason whenever the deployment cannot run — that is the whole point of
    the dry run.

    ``services`` resolves a node address to its service, the same resolver the
    deploy path takes. A plan asks one node one question through it — whether
    the machine that will load the model already holds the file — so the
    parameter exists for the same reason it does everywhere else here: a test
    hands one in rather than the module reaching for a daemon.
    """
    intent = resolve_intent(
        recipe_id,
        registry=get_registry(),
        engine=engine,
        variant=variant,
        model=model,
        params=params,
        extra_args=extra_args,
        nodes=nodes,
        solo=solo,
        name=name,
        deployment_id=deployment_id,
        allow_missing_model=allow_missing_model,
    )
    return realise(intent, services=services)


def realise(
    intent: RunIntent, services: Callable[[str], Any] | None = None
) -> state.DeployPlan:
    """How Docker runs ``intent``: the Docker backend's half of a plan.

    Where the model file sits inside the container (:func:`_resolve_model_file`,
    which asks the node that will load it), which API port the run gets beside
    what the nodes already serve, the rendered launch for those two, and then
    the containers — names, labels, mounts, limits — the image's presence and
    the memory budget beside the runs already on each node.
    """
    services = services or state.rank_services()
    warnings = list(intent.warnings)
    recipe = intent.recipe
    recipe_id = intent.recipe_id
    engine_obj = intent.engine
    node_list = intent.nodes
    topology = intent.topology
    image_ref = intent.image_ref
    rendezvous_port = intent.rendezvous_port
    rpc_port = intent.rpc_port

    # The rank that loads the model is rank zero, on every engine: the RPC
    # workers load nothing and a rendezvous rank reads its own shard from the
    # same snapshot. So one node is asked, and it is the head's.
    model_file, model_source = _resolve_model_file(
        engine_obj,
        recipe,
        intent.model,
        intent.model_present,
        node_list[0] if node_list else "",
        services,
        warnings,
    )

    if intent.port_pin is not None:
        port = intent.port_pin
    else:
        # The recipe's port, or none at all: a preference, moved when the
        # nodes this run occupies already hold it.
        port = _choose_port(
            intent.port_preference,
            node_list,
            intent.reserved_ports,
            intent.deployment_id,
            warnings,
        )
    merged = intent.params_for(port)
    # Rendered with the port the plan chose rather than the recipe's
    # preference it was moved off.
    ranks = intent.render(port, model_file)

    dep_id = intent.deployment_id
    generation = state._next_generation(dep_id)
    profile = _container_profile(engine_obj)
    mounts, cache_mounts = _build_mounts(engine_obj)
    network_host = bool(profile.get("network_host", False))
    # In solo the API port is published only when the container is not on the
    # host network; on host networking the engine already binds it directly.
    port_mappings = [] if network_host else [f"{port}:{port}"]
    created_at = state._now()
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

    rank_plans: list[state.RankPlan] = []
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
            state.RankPlan(
                rank=rank,
                # Empty means this machine, which is what a size-one
                # deployment has always carried.
                node=node_list[rank] if rank < len(node_list) else "",
                host=topology.nodes[rank].host,
                command=launch.command,
                script=launch.script,
                is_head=rank == 0,
                rpc_endpoints=list(rpc_endpoints) if rank == 0 else [],
                model_source=model_source if rank == 0 else "",
                model_path=model_file if rank == 0 else "",
                container=state.ContainerSpec(
                    image=image_ref,
                    name=state.rank_container_name(dep_id, rank, generation),
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

    image_present, image_size = state._inspect_image(image_ref, warnings)

    readiness = engine_obj.readiness_path()
    served_model = ""
    if readiness and readiness == engine_obj.models_path():
        served_model = _served_model_name(ranks[0].command) or intent.model

    fraction = _claimed_fraction(engine_obj, ranks[0].command, merged)
    budget = _memory_budget(node_list, fraction, dep_id, services)
    return state.DeployPlan(
        deployment_id=dep_id,
        recipe_id=str(recipe.get("id") or recipe_id),
        recipe_name=str(recipe.get("name") or recipe_id),
        name=intent.name,
        engine=intent.engine_name,
        variant=intent.variant,
        image_ref=image_ref,
        model=intent.model,
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
        mods=intent.mods,
        params=merged,
        extra_args=list(intent.extra_args),
        launch_command=ranks[0].command,
        ranks=[r.to_dict() for r in ranks],
        container=container,
        cache_mounts=cache_mounts,
        rank_plans=rank_plans,
        generation=generation,
        image_present=image_present,
        image_size_bytes=image_size,
        model_present=intent.model_present,
        model_source=model_source,
        model_path=model_file,
        model_patterns=engine_obj.model_patterns(recipe),
        served_model=served_model,
        gpu_memory_utilization=fraction,
        memory_budget=budget,
        serves=intent.serves,
        warnings=warnings,
        backend=state.BACKEND_NAME,
        intent=intent.to_dict(),
        intent_hash=intent.intent_hash(),
    )
