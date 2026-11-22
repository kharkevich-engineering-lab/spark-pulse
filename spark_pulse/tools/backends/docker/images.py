"""Getting a run's image onto its nodes: the registry relay, upstream pulls and their retries.

Moved out of ``tools/native_runtime.py`` (#163).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from spark_pulse import tools
from spark_pulse.tools.backends.docker import lifecycle, state
from spark_pulse.tools.docker import (
    PullCancelled,
    PullStalled,
)
from spark_pulse.tools.events import EventType

# What a run is, apart from how Docker runs it (#161). The helpers are
# re-exported so callers and tests that reach them here keep working.
# Capacity validation is pure arithmetic with no side effect to simulate, so
# it is imported directly, like ``spark_pulse.engines``.
# What a plan is, apart from how Docker runs it: credentials kept out of what
# a browser reads, the readiness question, and which nodes, ports and memory a
# run occupies. Re-exported under their old names so every caller and every
# test that reaches them here keeps working.
from spark_pulse.tools.runtime_errors import NativeRuntimeError

logger = logging.getLogger(__name__)


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


def _pull_targets(plan_obj: state.DeployPlan) -> list[str]:
    """Each distinct node the gang runs on, in start order.

    Workers pull first for the same reason they start first: rank zero should
    be the last thing that has to wait.
    """
    seen: list[str] = []
    for rank_plan in plan_obj.start_order():
        if rank_plan.node not in seen:
            seen.append(rank_plan.node)
    return seen


def _upstream_ref(plan_obj: state.DeployPlan) -> str:
    """The image the plan names, before any node is pointed at its own copy."""
    return plan_obj.image_ref or plan_obj.container.image


def _registry_copy(ref: str) -> str:
    """The name a node holds ``ref`` under when it came out of the control
    node's registry, or "" when ``ref`` carries no digest to find it by."""
    try:
        return str(tools.registry.loopback_reference(ref) or "")
    except Exception as exc:  # pragma: no cover — no registry configured
        logger.debug("could not compose the registry copy of %s: %s", ref, exc)
        return ""


def _image_on_node(docker: Any, ref: str) -> str:
    """The name ``ref`` is present under on this node, or "".

    Its own name first. Then the copy image sync leaves behind —
    ``127.0.0.1:<port>/<repo>@<digest>`` — which is the same bytes under the
    same digest, and which ``docker run ghcr.io/…@<digest>`` would not find:
    a digest reference is resolved per repository. Without this a synced
    node pulled the whole image again from its upstream.
    """
    if docker.image_exists(ref):
        return ref
    copy = _registry_copy(ref)
    if copy and docker.image_exists(copy):
        return copy
    return ""


def _run_from(plan_obj: state.DeployPlan, node: str, image: str) -> None:
    """Point every rank on ``node`` at the name the image has there."""
    for rank_plan in plan_obj.rank_plans:
        if rank_plan.node == node:
            rank_plan.container.image = image


def _relayed_source(node: str, ref: str) -> tuple[str, str | None] | None:
    """``(name, relay)`` to pull ``ref`` from the control node's registry.

    None when the registry does not hold it — a deploy is never pointed at a
    registry with nothing in it — or when a peer could not reach it. The
    control node pulls the loopback name directly: the registry is on its
    loopback already, and a relay there would collide with it.
    """
    copy = _registry_copy(ref)
    if not copy:
        return None
    try:
        if tools.registry.node_reference(ref) == ref:
            return None
        if state._rank_is_here({"node": node}):
            return copy, None
        relay = str(tools.registry.relay_target() or "")
    except Exception as exc:  # noqa: BLE001 — an unreachable registry is a no-op
        logger.debug("could not consult the registry for %s: %s", ref, exc)
        return None
    return (copy, relay) if relay else None


def _image_missing(services: Callable[[str], Any], plan_obj: state.DeployPlan) -> bool:
    """Whether this deploy has to pull before it can start anything.

    True when *any* node the gang runs on lacks the image. A daemon that will
    not answer is not evidence the image is absent, so an unreachable check
    keeps the deploy on the inline path where the failure is reported straight
    back to the caller.
    """
    for address in _pull_targets(plan_obj):
        try:
            if not _image_on_node(services(address), _upstream_ref(plan_obj)):
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


#: How a registry or daemon says an image has no build for this node's
#: platform: Podman's "no image found in image index for architecture amd64",
#: Docker's "no matching manifest for linux/amd64". No retry changes that,
#: and three attempts with backoff only delayed the answer (#172).
_NO_PLATFORM_MARKERS = (
    "no image found in image index for architecture",
    "no matching manifest for",
)


def pull_is_final(exc: Exception) -> bool:
    """Whether a pull failure is one another attempt cannot fix."""
    text = str(exc).lower()
    return any(marker in text for marker in _NO_PLATFORM_MARKERS)


def pull_progress_message(ref: str, snapshot: dict[str, Any]) -> str:
    """One progress line for the run's timeline: a whole percentage and how
    much has moved, not the raw float the pull reports (``49.12233720868858%``).
    """
    try:
        percent = float(snapshot.get("percent") or 0)
    except (TypeError, ValueError):
        percent = 0.0
    done = int(snapshot.get("bytes_done") or 0)
    total = int(snapshot.get("bytes_total") or 0)
    moved = (
        f" ({done / 1e9:.1f} of {total / 1e9:.1f} GB)"
        if total
        else (f" ({done / 1e9:.1f} GB)" if done else "")
    )
    return f"pulling {ref}: {int(percent)}%{moved}"


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
            state.publish_event(
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
            if lifecycle._teardown_requested(dep_id):
                raise PullCancelled(f"pull of {ref} cancelled") from exc
            if attempt < PULL_ATTEMPTS and not pull_is_final(exc):
                delay = PULL_RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(PULL_RETRY_BACKOFF_SECONDS) - 1)
                ]
                message = (
                    f"pull of {ref} on {where} failed ({exc}); retrying in "
                    f"{delay:g}s — attempt {attempt + 1} of {PULL_ATTEMPTS}"
                )
                logger.warning(message)
                state.publish_event(
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
            state.publish_event(
                EventType.IMAGE_PULL_FAILED,
                dep_id,
                message,
                {**last, "image_ref": ref, "node": where},
            )
            raise NativeRuntimeError(message) from exc
    # Unreachable: the loop either returns or raises on every path.
    raise NativeRuntimeError(f"could not pull image {ref} on {where}")


def _pull_from_registry(
    docker: Any,
    dep_id: str,
    node: str,
    ref: str,
    where: str,
    progress: Callable[[dict[str, Any]], None],
) -> tuple[str, Any]:
    """Pull ``ref`` out of the control node's registry, when it holds it.

    Returns ``(name pulled, outcome)``, or ``("", None)`` when the registry
    is not a source for this node or the pull from it failed — in which case
    the caller pulls from upstream exactly as it always has. Slow beats
    failed: an agent too old to open a relay, a registry gone quiet, are
    reasons to take the long way, not to fail the deploy.
    """
    source = _relayed_source(node, ref)
    if source is None:
        return "", None
    copy, relay = source
    try:
        outcome = docker.pull_image(
            copy,
            progress,
            cancel=lambda: _pull_cancel_requested(dep_id),
            relay=relay,
        )
        return copy, outcome
    except PullCancelled:
        raise
    except Exception as exc:
        if lifecycle._teardown_requested(dep_id):
            raise PullCancelled(f"pull of {copy} cancelled") from exc
        message = (
            f"could not pull {copy} from the control node's registry on "
            f"{where} ({exc}); pulling {ref} from its own registry instead"
        )
        logger.warning(message)
        state.publish_event(
            EventType.IMAGE_PULL_PROGRESS,
            dep_id,
            message,
            {"image_ref": ref, "node": where},
        )
        return "", None


def _pull_image_if_missing(
    docker: Any, plan_obj: state.DeployPlan, node: str = ""
) -> bool:
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
    ref = _upstream_ref(plan_obj)
    where = node or getattr(docker, "label", "") or "this machine"
    try:
        present = _image_on_node(docker, ref)
        if present:
            _run_from(plan_obj, node, present)
            return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("image presence check failed for %s: %s", ref, exc)

    state._update_record(dep_id, status="pulling")
    state.publish_event(
        EventType.IMAGE_PULL_STARTED,
        dep_id,
        f"pulling {ref} on {where}",
        {"image_ref": ref, "percent": 0.0, "node": where},
    )

    last: dict[str, Any] = {}

    def _progress(snapshot: dict[str, Any]) -> None:
        last.update(snapshot)
        state.publish_event(
            EventType.IMAGE_PULL_PROGRESS,
            dep_id,
            pull_progress_message(ref, snapshot),
            {"image_ref": ref, "node": where, **snapshot},
        )

    _register_pull(dep_id)
    try:
        pulled, result = _pull_from_registry(
            docker, dep_id, node, ref, where, _progress
        )
        if not pulled:
            result = _pull_with_retries(docker, dep_id, ref, where, _progress, last)
            pulled = ref
    finally:
        _unregister_pull(dep_id)
    _run_from(plan_obj, node, pulled)

    state.publish_event(
        EventType.IMAGE_PULL_COMPLETED,
        dep_id,
        f"pulled {pulled}",
        {
            "image_ref": pulled,
            "node": where,
            **(result if isinstance(result, dict) else {}),
        },
    )
    state._update_record(dep_id, status="starting", image_present=True)
    return True
