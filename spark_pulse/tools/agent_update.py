"""Updating a node's agent over its own stream, and keeping them current.

The agent is updated the way every other node operation happens: the control
plane sends it a command — here the bundle it would otherwise have shipped over
SSH — and the agent installs it and restarts itself. SSH is left for the first
bootstrap only; a node that already runs an agent never needs it again to move
to a new version.

Two entry points:

* :func:`update_node` pushes the current bundle to one connected node and
  reports what came back. The Update action on the Cluster page calls it.
* :func:`start_updater` / :func:`stop_updater` run a background thread that,
  when ``agent_auto_update`` is on, pushes the bundle to any connected peer
  whose running binary is not the one this control plane ships — decided by
  the binary's digest, which cannot lie the way a version string can.

The control node is never a target: it runs its agent as a child of the
control-plane process, not from a unit, so it moves to a new version when its
package does, not over the stream.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["update_node", "stale_peers", "start_updater", "stop_updater"]

#: How often the background updater looks for stale agents.
CHECK_INTERVAL_SECONDS = 300.0

_thread: threading.Thread | None = None
_stop = threading.Event()


#: The agent's own answer to a command whose `op` oneof it does not recognise
#: (`agent/src/executor/mod.rs`). An agent too old to know `install_bundle`
#: sees the field as unknown and the oneof as empty, so this is exactly how a
#: pre-self-update agent reports "I cannot be updated over my stream".
_UNKNOWN_OP_MESSAGE = "command carries no op"


def _is_unknown_op(exc: Any) -> bool:
    """Whether ``exc`` is the agent saying it did not recognise the operation."""
    return (
        getattr(exc, "error_type", "") == "ValueError"
        and getattr(exc, "error_message", "") == _UNKNOWN_OP_MESSAGE
    )


def _bundle_for(target: str):
    from spark_pulse.agent.bundle import build_bundle
    from spark_pulse.agent import runtime as agent_runtime

    current = agent_runtime.current()
    cache = current.server.directory / "bundles" if current is not None else None
    return build_bundle(target=target, cache_dir=cache)


def update_node(node: Any) -> dict[str, Any]:
    """Push this control plane's agent bundle to ``node`` over its stream.

    Returns a report dict. Raises nothing for an expected failure — an
    unreachable node or a refusing agent is recorded in the report — so a
    caller iterating nodes is never stopped by one.
    """
    from spark_pulse.agent.bundle import DEFAULT_TARGET, MissingAgentBinary
    from spark_pulse.agent.errors import NodeOperationError
    from spark_pulse.tools import node_service

    base = {"node_id": node.id, "name": node.name or node.address, "updated": False}
    if node.is_control_plane:
        base["detail"] = (
            "the control node runs its agent from the control-plane process; it "
            "updates when the package does, not over the stream"
        )
        return base
    try:
        bundle = _bundle_for(DEFAULT_TARGET)
    except MissingAgentBinary as exc:
        base["detail"] = str(exc)
        return base
    try:
        target = node_service.node_for(node.address, ssh_user=node.ssh_user)
        service = node_service.service_for(target)
        result = service.install_bundle(bundle.data, bundle.name, bundle.version)
    except node_service.NoAgent as exc:
        base["detail"] = str(exc)
        return base
    except NodeOperationError as exc:
        # An agent older than the self-update op cannot receive it: it does not
        # know the `install_bundle` field, so the whole `op` reads as unset and
        # it answers "command carries no op". Self-update is chicken-and-egg —
        # the very capability to accept the update is what is missing — so this
        # first hop has to go over SSH. `needs_reinstall` tells the caller to
        # reinstall over the control-plane key already in the node's
        # `authorized_keys`, which is bootstrap-class work and the one place SSH
        # still belongs.
        if _is_unknown_op(exc):
            base["needs_reinstall"] = True
            base["detail"] = (
                "this agent predates stream self-update, so it cannot update "
                "itself; reinstall it over SSH to bring it current"
            )
            return base
        base["detail"] = str(exc)
        return base
    except Exception as exc:  # the agent ran it and it failed — reachable
        base["detail"] = str(exc)
        return base
    return {
        **base,
        "updated": True,
        "version": result.version,
        "path": result.path,
        "restarting": result.restarting,
        "detail": "staged and restarting onto the new binary",
    }


def stale_peers() -> list[Any]:
    """Connected peer nodes whose running binary is not the one we ship.

    Decided by the reported ``binary_sha256`` against the packaged digests.
    A node that reports no digest (an agent too old to send one) counts as
    stale, because that is exactly the version this exists to move past.
    """
    from spark_pulse.agent.bundle import packaged_digests
    from spark_pulse.agent import runtime as agent_runtime
    from spark_pulse.tools import node_registry

    current = agent_runtime.current()
    if current is None:
        return []
    ours = set(packaged_digests().values())
    stale: list[Any] = []
    for node in node_registry.list_nodes():
        if node.is_control_plane:
            continue
        connection = current.hub.get(node.id)
        if connection is None:
            continue
        digest = str(getattr(connection.facts, "binary_sha256", "") or "")
        if not digest or digest not in ours:
            stale.append(node)
    return stale


def _auto_update_on() -> bool:
    from spark_pulse.config import config

    return bool(config.agent_auto_update)


def _run() -> None:
    while not _stop.wait(timeout=CHECK_INTERVAL_SECONDS):
        try:
            if not _auto_update_on():
                continue
            for node in stale_peers():
                report = update_node(node)
                if report.get("updated"):
                    logger.info("auto-updated agent on %s", report["name"])
                else:
                    logger.debug(
                        "auto-update skipped %s: %s",
                        report["name"],
                        report.get("detail"),
                    )
        except Exception as exc:  # a background loop must never die
            logger.debug("agent auto-update sweep failed: %s", exc)


def start_updater() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="agent-auto-update", daemon=True)
    _thread.start()
    logger.info(
        "agent auto-update sweeper started (every %.0fs)", CHECK_INTERVAL_SECONDS
    )


def stop_updater() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2)
