"""A run's lifecycle on Docker: create, launch, wait, tear down, and reconcile.

Moved out of ``tools/native_runtime.py`` (#163), which re-exports it.

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

import logging
import os
import shlex
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from spark_pulse import tools
from spark_pulse.config import config
from spark_pulse.tools.backends.docker import images, planning, state
from spark_pulse.tools.docker import (
    ContainerMetadata,
    PullCancelled,
)
from spark_pulse.tools.events import EventType

# What a run is, apart from how Docker runs it (#161). The helpers are
# re-exported so callers and tests that reach them here keep working.
from spark_pulse.tools.labels import (
    DEPLOYMENT_LABEL,
    GENERATION_LABEL,
    RANK_LABEL,
    WORLD_SIZE_LABEL,
)

# Capacity validation is pure arithmetic with no side effect to simulate, so
# it is imported directly, like ``spark_pulse.engines``.
# What a plan is, apart from how Docker runs it: credentials kept out of what
# a browser reads, the readiness question, and which nodes, ports and memory a
# run occupies. Re-exported under their old names so every caller and every
# test that reaches them here keeps working.
from spark_pulse.tools.readiness import (
    _models_answering,
    probe_ready,
)
from spark_pulse.tools.runtime_errors import NativeRuntimeError

logger = logging.getLogger(__name__)


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
    plan_obj: state.DeployPlan,
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
        remote = f"{state.MODS_DIR}/{name}"
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


def _deploy_script(docker: Any, rank_plan: state.RankPlan) -> None:
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
        if not docker.copy_to_container(name, local_path, state.SCRIPT_PATH):
            raise NativeRuntimeError(f"could not copy the launch script into {name}")
    finally:
        try:
            os.unlink(local_path)
        except OSError:  # pragma: no cover - defensive
            pass

    docker.exec_in_container(name, ["chmod", "0755", state.SCRIPT_PATH], user="root")
    docker.exec_in_container(
        name,
        ["bash", "-lc", f"bash {shlex.quote(state.SCRIPT_PATH)} >> /proc/1/fd/1 2>&1"],
        detach=True,
    )


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
    patterns = [p for p in (_self_excluding_pattern(state.SCRIPT_PATH),) if p]
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
    plan_obj: state.DeployPlan,
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
        waiting: list[state.RankPlan] = []
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
    docker: Any, rank_plan: state.RankPlan, status: dict[str, Any]
) -> str:
    name = rank_plan.container.name
    code = _exit_code_of(status)
    coda = f" with exit code {code}" if code is not None else ""
    return (
        f"container {name} on {rank_plan.node or 'this machine'} "
        f"exited before the engine became ready{coda} "
        f"({status.get('status')}). Last log lines:\n{_log_tail(docker, name)}"
    )


def _serve_process_gone_message(docker: Any, rank_plan: state.RankPlan) -> str:
    name = rank_plan.container.name
    program = serve_program(rank_plan.command) or "the engine"
    return (
        f"the serve process in {name} on {rank_plan.node or 'this machine'} "
        f"is gone: {program} exited before the engine became ready, and the "
        "container is still up because what it runs is a keepalive — the "
        f"engine is an exec inside it. Last log lines:\n{_log_tail(docker, name)}"
    )


def _readiness_deadline_message(
    services: Callable[[str], Any], plan_obj: state.DeployPlan, timeout: int
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
    if plan_obj.served_model:
        # The port answered all along, for somebody else: that is the reason,
        # and the log tail of an engine that could not bind would only hint it.
        answered = _models_answering(plan_obj.readiness_url)
        if answered and plan_obj.served_model not in answered:
            message += (
                f"; port {plan_obj.port} answered for {', '.join(answered)}, "
                f"not {plan_obj.served_model} — another server holds it"
            )
    try:
        tail = _log_tail(services(head.node), head.container.name)
    except Exception as exc:  # pragma: no cover - a node that stopped answering
        logger.debug("could not read the logs of %s: %s", head.container.name, exc)
        return message
    return f"{message}. Last log lines:\n{tail}" if tail.strip() else message


def _wait_ready(
    services: Callable[[str], Any],
    plan_obj: state.DeployPlan,
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
        if probe_ready(plan_obj.readiness_url, model=plan_obj.served_model):
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
    timeout = state.CONFIRM_GONE_TIMEOUT if timeout is None else timeout
    interval = state.CONFIRM_GONE_INTERVAL if interval is None else interval
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
            f"within {state.CONFIRM_GONE_TIMEOUT:g}s; a rank of an earlier attempt "
            "is still holding the GPU, so this one will not be started"
        )


def _stale_names(
    docker: Any, plan_obj: state.DeployPlan, rank_plan: state.RankPlan
) -> list[str]:
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
    services: Callable[[str], Any], plan_obj: state.DeployPlan
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
        "since": state._now(),
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


def _fail_launched_gang(
    services: Callable[[str], Any], plan_obj: state.DeployPlan, message: Any
) -> dict[str, Any] | None:
    """Settle a gang that launched and never became ready: down, then ``error``.

    Each rank's container runs a keepalive and the engine is an exec inside
    it, so an engine that dies leaves its container up — and on a cluster the
    other ranks with it, a worker blocked in a rendezvous nobody will join. On
    the two-node cluster every failed run left both ranks running on both
    machines until somebody stopped them by hand. A launch failure already
    tore the gang down (``_abort``); a readiness failure is the same verdict
    later. Each rank's last log lines are kept first, as ``_abort`` keeps
    them, because the teardown removes the container that held them.

    Caller holds the lifecycle lock and has checked the record is not torn
    down.
    """
    entries = [state._rank_record(r) for r in plan_obj.rank_plans]
    orphans = _teardown_entries(services, entries)
    return state._record_error(
        plan_obj.deployment_id,
        message,
        orphans=orphans,
        final_logs={
            str(e.get("rank", 0)): e["final_logs"]
            for e in entries
            if e.get("final_logs")
        },
    )


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
    record = state.get_deployment(deployment_id)
    return record is None or str(record.get("status")) == "stopped"


def _teardown_requested(deployment_id: str) -> bool:
    """Whether the create path should abort rather than launch anything.

    Two signals cover two moments. The pull-cancel flag is set while a stop
    races an in-flight pull, before the record has changed. Once the stop has
    actually run it is the record that says so — :func:`_is_torn_down`. A
    background start re-reads this at each phase boundary so it never builds a
    container for a record that is on its way out.
    """
    return images._pull_cancel_requested(deployment_id) or _is_torn_down(deployment_id)


def persist_planned_record(plan_obj: state.DeployPlan, status: str) -> dict[str, Any]:
    """Write the plan's record at ``status``, replacing any earlier one.

    Split out of :func:`start` so a deploy that has to pull first can put the
    record on disk in ``pulling`` *before* the POST returns, rather than the
    caller seeing nothing until a 26 GB download finishes.
    """
    record = state._record_from_plan(plan_obj, status)
    with tools.deployment_records.transaction():
        records = [
            r for r in state._load_records() if r.get("id") != plan_obj.deployment_id
        ]
        records.append(record)
        state._save_records(records)
    return record


def _create_rank(
    docker: Any,
    plan_obj: state.DeployPlan,
    rank_plan: state.RankPlan,
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
            unmade = docker.ensure_directories(
                planning._bind_sources_to_create(spec.mounts)
            )
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

    state.publish_event(
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
        state.publish_event(
            EventType.DEPLOYMENT_MODS_APPLIED,
            dep_id,
            f"applied {len(applied)}/{len(plan_obj.mods)} mod(s)",
            {"mods": applied, "warnings": warnings, "rank": rank_plan.rank},
        )


def _launch_rank(
    docker: Any, plan_obj: state.DeployPlan, rank_plan: state.RankPlan
) -> None:
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
    plan_obj: state.DeployPlan,
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
    services = services or state.rank_services(docker)
    dep_id = plan_obj.deployment_id
    spec = plan_obj.container
    warnings = list(plan_obj.warnings)

    record = persist_planned_record(plan_obj, initial_status)

    state.publish_event(
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
        updated = state._record_error(
            dep_id,
            message,
            orphans=orphans or [],
            final_logs=final_logs or {},
        )
        return updated or {
            **record,
            "status": "error",
            "error_message": state._error_text(message),
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
        for address in images._pull_targets(plan_obj):
            images._pull_image_if_missing(services(address), plan_obj, address)
    except PullCancelled:
        # A stop or delete reached into the pull. That is not a failure to
        # report: the record is already being torn down, and marking it
        # "error" would leave a deliberate teardown looking like a crash.
        message = f"pull of {spec.image} cancelled by teardown"
        state.publish_event(EventType.DEPLOYMENT_STOPPED, dep_id, message)
        return state._update_record(
            dep_id, status="stopped", stopped_at=state._now()
        ) or {
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
    touched: list[state.RankPlan] = []

    def _abort(rank_plan: state.RankPlan, exc: Exception, phase: str) -> dict[str, Any]:
        # The rank that failed may itself have a container: run_container can
        # succeed and a mod or the script copy fail after it.
        pending = [*touched] if rank_plan in touched else [*touched, rank_plan]
        entries = [state._rank_record(r) for r in pending]
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
            _teardown_entries(services, [state._rank_record(r) for r in touched])
        message = f"deploy of {dep_id} cancelled by teardown"
        state.publish_event(EventType.DEPLOYMENT_STOPPED, dep_id, message)
        return state._update_record(
            dep_id, status="stopped", stopped_at=state._now()
        ) or {
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
        started = state._now()
        launched = state._update_record(
            dep_id, status="starting", started_at=started, warnings=warnings
        )
        state.publish_event(
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
                return state.get_deployment(dep_id) or {**record, "status": "stopped"}
            return _fail_launched_gang(services, plan_obj, exc) or {
                **record,
                "status": "error",
                "error_message": state._error_text(exc),
            }

    with _lifecycle_lock(dep_id):
        if _is_torn_down(dep_id):
            return state.get_deployment(dep_id) or {**record, "status": "stopped"}
        state.publish_event(
            EventType.DEPLOYMENT_READY,
            dep_id,
            f"{plan_obj.recipe_id} is serving on port {plan_obj.port}",
            {"port": plan_obj.port, "readiness_url": plan_obj.readiness_url},
        )
        return (
            state._update_record(
                dep_id, status="running", ready_at=state._now(), error_message=None
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
    plan_obj = planning.plan(
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

    services = state.rank_services()

    def _watch() -> None:
        dep_id = plan_obj.deployment_id
        try:
            _wait_ready(services, plan_obj, config.deploy_ready_timeout_seconds)
        except NativeRuntimeError as exc:
            # A stop or delete may have settled this record while we waited. A
            # readiness timeout after a deliberate teardown is not a crash, and
            # writing "error" over "stopped" — or resurrecting a deleted record
            # — would report one. Leave the teardown's verdict standing.
            with _lifecycle_lock(dep_id):
                if _is_torn_down(dep_id):
                    return
                _fail_launched_gang(services, plan_obj, exc)
            return
        # Readiness is observed here, so it is written here: this thread owns
        # the record from the launch to the first answer, and until it wrote
        # one the row said "running" from the moment the script was exec'd.
        with _lifecycle_lock(dep_id):
            if _is_torn_down(dep_id):
                return
            state._update_record(
                dep_id, status="running", ready_at=state._now(), error_message=None
            )
            state.publish_event(
                EventType.DEPLOYMENT_READY,
                dep_id,
                f"{plan_obj.recipe_id} is serving on port {plan_obj.port}",
                {"port": plan_obj.port},
            )

    if images._image_missing(services, plan_obj):
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
                    state._record_error(
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
    if state.get_deployment(deployment_id) is None:
        return None
    # A deployment still pulling has no container to stop; what has to stop is
    # the download. Ask before taking the lock — the pull runs outside it — so
    # a start racing this stop sees the cancel and aborts rather than launching
    # a container the teardown below would never see.
    images.cancel_pull(deployment_id)
    # The lock serialises this teardown against a create's launch phase: the
    # two cannot interleave, so we never tear down a half-created gang and then
    # have the create launch the other half behind us.
    with _lifecycle_lock(deployment_id):
        record = state.get_deployment(deployment_id)
        if record is None:
            return None
        services = services or state.rank_services(docker)
        entries = state.rank_entries(record)
        orphans = _teardown_entries(services, entries)
        names = ", ".join(str(e.get("container_name") or "") for e in entries)
        state.publish_event(
            EventType.DEPLOYMENT_STOPPED, deployment_id, f"stopped {names}"
        )
        return state._update_record(
            deployment_id,
            status="stopped",
            stopped_at=state._now(),
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
    if state.get_deployment(deployment_id) is None:
        return False
    # Set the pull-cancel flag before the lock, for the same reason stop does:
    # the pull runs outside the lock, so a racing start must be able to see the
    # cancel and abort rather than launch a container for a record we drop.
    images.cancel_pull(deployment_id)
    # The whole stop-then-drop runs under the lifecycle lock — re-entrant, so
    # the nested stop takes it again on this thread — so a create cannot slip a
    # freshly launched container in between the teardown and the record drop.
    with _lifecycle_lock(deployment_id):
        if state.get_deployment(deployment_id) is None:
            return False
        # Always tear the containers down, whatever the record says: a
        # deployment that errored during readiness still has a container
        # running.
        stopped = stop_deployment(deployment_id, docker=docker, services=services)
        if stopped is not None and stopped.get("orphans"):
            return False
        with tools.deployment_records.transaction():
            records = state._load_records()
            remaining = [r for r in records if r.get("id") != deployment_id]
            if len(remaining) == len(records):
                return False
            state._save_records(remaining)
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
    record = state.get_deployment(deployment_id)
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
    services = services or state.rank_services(docker)
    entry = next(
        (e for e in state.rank_entries(record) if int(e.get("rank", 0)) == rank), None
    )
    if entry is None:
        return f"Deployment has no rank {rank}"
    name = str(entry.get("container_name") or state.container_name_for(deployment_id))
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
    workers = min(len(entries), state.RANK_STATUS_MAX_WORKERS)
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
    record = state.get_deployment(deployment_id)
    if record is None:
        return None
    services = services or state.rank_services(docker)
    ranks = _gather_rank_statuses(services, state.rank_entries(record))
    container = ranks[0]["container"]
    port = record.get("port")
    url = record.get("readiness_url") or (f"http://127.0.0.1:{port}/v1/models")
    ready = bool(container.get("running")) and probe_ready(
        url, model=str(record.get("served_model") or "")
    )
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
    records = state._load_records()
    native = [r for r in records if r.get("runtime") == state.RUNTIME_NAME]
    try:
        docker = docker or state._docker_service()
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
        for entry in state.rank_entries(record):
            if not state._rank_is_here(entry):
                continue
            container = by_name.get(str(entry.get("container_name") or ""))
            if container is None or container.status not in ("running", "created"):
                record["status"] = "stopped"
                record.setdefault("stopped_at", state._now())
                changed = True
                break

    for container in containers:
        if not container.name.startswith(state.CONTAINER_PREFIX):
            continue
        dep_id = container.labels.get(DEPLOYMENT_LABEL) or container.name.removeprefix(
            state.CONTAINER_PREFIX
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
            "created_at": container.metadata.created_at or state._now(),
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
            "runtime": state.RUNTIME_NAME,
            # Adopted from a container: the backend is known, the intent that
            # started it is not, so there is no hash to claim.
            "backend": state.BACKEND_NAME,
            "intent": None,
            "intent_hash": None,
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
        state._save_records(records)

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
    services = services or state.rank_services(docker)
    records = state._load_records()
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
        state._save_records(records)
    return cleared
