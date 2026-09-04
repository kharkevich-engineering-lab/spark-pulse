"""Engine image catalogue, pull jobs and node distribution.

The engine registry says which images *should* exist (bundled specs plus any
published engine index); the Docker daemon says which ones actually do. This
module joins the two so the UI can answer three questions the first hardware
deploy could not:

* Is the image this recipe needs already on the host, or is a 26 GB download
  hiding behind the deploy button?
* Is the local copy of a version still the one the index advertises?
  Republishing an engine version changes its digest, so a host that pulled
  ``0.1.0`` yesterday can silently be running something else today. That is
  **digest drift** and it is reported per entry.
* Do the other nodes have it?

Pull jobs mirror :mod:`spark_pulse.tools.models`: a background thread, a job
record, and aggregated progress events on the shared broadcaster.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from spark_pulse.engines import get_registry
from spark_pulse.tools.docker import PullCancelled, split_ref
from spark_pulse.tools.events import DeploymentEvent, EventType
from spark_pulse.tools.ssh import OpenSSHClient, SSHClient, SSHError

logger = logging.getLogger(__name__)

TERMINAL_STATES = ("completed", "failed", "cancelled")

EVENT_QUEUED = EventType.IMAGE_PULL_QUEUED
EVENT_STARTED = EventType.IMAGE_PULL_STARTED
EVENT_PROGRESS = EventType.IMAGE_PULL_PROGRESS
EVENT_COMPLETED = EventType.IMAGE_PULL_COMPLETED
EVENT_FAILED = EventType.IMAGE_PULL_FAILED
EVENT_CANCELLED = EventType.IMAGE_PULL_CANCELLED
EVENT_DELETED = EventType.IMAGE_DELETED
EVENT_SYNCED = EventType.IMAGE_SYNCED

RESOURCE_TYPE = "image"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Event publishing ─────────────────────────────────────────────────────────
#
# Same arrangement as the model catalogue: pull jobs run on worker threads
# while the broadcaster is asyncio-based, so ``/sse/images`` registers its loop
# here and publishes are marshalled onto it. No listener means nothing to
# deliver, which is not an error.

_loop: asyncio.AbstractEventLoop | None = None


def register_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Record the loop that SSE consumers run on (called from ``sse.py``)."""
    global _loop
    _loop = loop


def publish_event(
    event_type: EventType, resource: str, metadata: dict[str, Any]
) -> None:
    """Emit an image event on the shared broadcaster from any thread."""
    from spark_pulse.sse import _get_event_broadcaster

    event = DeploymentEvent(
        event_type=event_type,
        resource=resource,
        resource_type=RESOURCE_TYPE,
        message=event_type.value,
        metadata=metadata,
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


# ── Catalogue ────────────────────────────────────────────────────────────────


def _docker() -> Any:
    """The container service for this process (real or mock)."""
    from spark_pulse import tools

    return tools.docker._get_service()


def local_digest(info: dict[str, Any] | None, repository: str) -> str:
    """Pick the registry digest of a local image for ``repository``.

    ``RepoDigests`` looks like ``["ghcr.io/org/img@sha256:..."]``; entries for
    other repositories (a retagged image) are ignored.
    """
    for entry in (info or {}).get("repo_digests") or []:
        repo, _, digest = str(entry).partition("@")
        if digest and (not repository or repo == repository):
            return digest
    return ""


def registry_base() -> str:
    """The control node's registry base, or "" when it cannot be worked out."""
    from spark_pulse import tools

    try:
        return str(tools.registry.load_settings().base)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("could not resolve the local registry base: %s", exc)
        return ""


def location_fields(ref: str, digest: str, base: str) -> dict[str, Any]:
    """The three-field location of an image plus the composed pull reference.

    ``registry_base``, ``repository`` and ``digest`` are kept apart because
    only the first of them changes per node: a worker that pulled from the
    control node holds ``<control>:5000/owner/repo@sha256:D`` while the index
    calls it ``ghcr.io/owner/repo@sha256:D``, same digest either way. One
    opaque string cannot express that, so the catalogue carries the parts and
    composes the reference for whichever host is doing the pulling.
    """
    from spark_pulse import tools

    upstream = tools.registry.location_for(ref, digest)
    local = tools.registry.ImageLocation(
        registry_base=base or upstream.registry_base,
        repository=upstream.repository,
        digest=upstream.digest,
    )
    return {
        "location": local.to_dict(),
        "upstream_location": upstream.to_dict(),
        # What a node is told to pull. Anonymous: no credential travels with it.
        "pull_ref": local.reference() if local.digest else ref,
    }


def _spec_entry(spec: Any, docker: Any, base: str = "") -> dict[str, Any]:
    """Build one catalogue entry from an engine spec plus local state."""
    ref = spec.image_ref
    repository, tag = split_ref(ref)
    advertised = str(spec.digest or "")

    info = docker.image_info(ref)

    # Digest drift is a property of the *tag*: the index advertises a digest
    # for a version, and the tag on this host may resolve to a different one
    # after a republish. When the spec is pinned by digest the ref itself is
    # immutable, so the tagged image is what has to be compared.
    tag_name = str(spec.tag or spec.version or "latest")
    if "/" in tag_name or ":" in tag_name:
        tag_name = split_ref(tag_name)[1]
    tagged_ref = f"{spec.image}:{tag_name}" if spec.image else ref
    tag_info = docker.image_info(tagged_ref) if tagged_ref != ref else info
    tag_digest = local_digest(tag_info, spec.image or repository)

    # A digest-pinned ref is rarely a local name: what the host actually holds
    # is the tag. It counts as present when its digest is the advertised one.
    if info is None and advertised and tag_digest == advertised:
        info = tag_info
    present = info is not None

    digest = local_digest(info, repository) or tag_digest
    # The advertised content already being here is what matters to a deploy:
    # the tag pointing somewhere older is then only a naming detail, not an
    # update. Reporting drift in that case contradicts the digests shown
    # beside it.
    advertised_is_local = bool(advertised) and advertised in (digest, tag_digest)
    drift = (
        bool(advertised and tag_digest and tag_digest != advertised)
        and not advertised_is_local
    )

    return {
        "ref": ref,
        "repository": repository,
        "tag": tag,
        "tagged_ref": tagged_ref,
        "engine": spec.engine,
        "variant": spec.variant,
        "engine_key": spec.key,
        "version": spec.version,
        "legacy_tags": list(spec.legacy_tags),
        "source": spec.source,
        "description": spec.description,
        "present": present,
        "image_id": (info or {}).get("id", ""),
        "size_bytes": int((info or {}).get("size_bytes") or 0),
        "created": (info or {}).get("created"),
        "local_digest": digest,
        "index_digest": advertised,
        "digest_drift": drift,
        "update_available": drift or not present,
        "tag_is_stale": bool(advertised and tag_digest and tag_digest != advertised),
        **location_fields(ref, advertised or digest, base),
    }


def _known_repositories(specs: list[Any]) -> set[str]:
    return {s.image for s in specs if s.image}


def _untracked_entries(
    docker: Any, specs: list[Any], claimed: set[str], base: str = ""
) -> list[dict[str, Any]]:
    """Local images in an engine repository that no spec claims.

    An image left behind by a superseded engine version still occupies 26 GB;
    it belongs in the catalogue so it can be deleted.
    """
    repositories = _known_repositories(specs)
    if not repositories:
        return []
    try:
        local = docker.list_images()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("could not list local images: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for image in local:
        for ref in image.get("repo_tags") or []:
            repository, tag = split_ref(str(ref))
            if repository not in repositories or ref in claimed:
                continue
            out.append(
                {
                    "ref": ref,
                    "repository": repository,
                    "tag": tag,
                    "tagged_ref": ref,
                    "engine": _engine_for_repository(specs, repository),
                    "variant": "",
                    "engine_key": "",
                    "version": tag,
                    "legacy_tags": [],
                    "source": "local",
                    "description": "",
                    "present": True,
                    "image_id": image.get("id", ""),
                    "size_bytes": int(image.get("size_bytes") or 0),
                    "created": image.get("created"),
                    "local_digest": local_digest(image, repository),
                    "index_digest": "",
                    "digest_drift": False,
                    "update_available": False,
                    **location_fields(str(ref), local_digest(image, repository), base),
                }
            )
    return out


def _engine_for_repository(specs: list[Any], repository: str) -> str:
    for spec in specs:
        if spec.image == repository:
            return spec.engine
    return ""


def list_images() -> list[dict[str, Any]]:
    """Return the engine image catalogue, engine entries first."""
    docker = _docker()
    try:
        specs = [s for s in get_registry().list() if s.image]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("engine registry unavailable: %s", exc)
        specs = []

    base = registry_base()
    entries = [_spec_entry(spec, docker, base) for spec in specs]
    claimed = {e["ref"] for e in entries} | {e["tagged_ref"] for e in entries}
    entries.extend(_untracked_entries(docker, specs, claimed, base))
    entries.sort(key=lambda e: (e["source"] == "local", e["ref"]))
    return entries


def get_image(ref: str) -> dict[str, Any] | None:
    """Return a single catalogue entry by ref, or None."""
    for entry in list_images():
        if entry["ref"] == ref or entry["tagged_ref"] == ref:
            return entry
    return None


def presence(
    ref: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = 30,
    client: SSHClient | None = None,
) -> dict[str, Any]:
    """Report which nodes carry ``ref``, and with which image ID."""
    docker = _docker()
    info = docker.image_info(ref) or {}
    local_id = str(info.get("id") or "")
    ssh = client or _make_ssh_client(ssh_user)
    inspect = f"docker image inspect {shlex.quote(ref)} --format '{{{{.Id}}}}'"

    def _one(node: str) -> dict[str, Any]:
        try:
            result = ssh.exec(node, inspect, timeout=timeout)
        except (SSHError, OSError) as exc:
            return {
                "node": node,
                "present": False,
                "image_id": "",
                "matches": False,
                "error": str(exc)[:500],
            }
        remote_id = (result.stdout or "").strip()
        return {
            "node": node,
            "present": bool(result.returncode == 0 and remote_id),
            "image_id": remote_id,
            "matches": bool(remote_id and remote_id == local_id),
            "error": None,
        }

    results: list[dict[str, Any]] = []
    if nodes:
        with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
            results = list(pool.map(_one, nodes))
    return {
        "ref": ref,
        "local": bool(local_id),
        "image_id": local_id,
        "nodes": results,
    }


# ── Pull jobs ────────────────────────────────────────────────────────────────

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_cancelled: set[str] = set()


def _publish_job(event: EventType, job: dict[str, Any]) -> None:
    publish_event(event, str(job.get("id", "")), dict(job))


def _set_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job.update(fields)
        return dict(job)


def list_pulls() -> list[dict[str, Any]]:
    """Return all known pull jobs, newest first."""
    with _jobs_lock:
        jobs = [dict(j) for j in _jobs.values()]
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return jobs


def get_pull(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def start_pull(ref: str) -> dict[str, Any]:
    """Queue an image pull and run it on a background thread."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("ref is required")
    repository, tag = split_ref(ref)

    for job in list_pulls():
        if job.get("ref") == ref and job.get("status") in ("queued", "running"):
            return job

    job_id = uuid.uuid4().hex[:12]
    job: dict[str, Any] = {
        "id": job_id,
        "ref": ref,
        "repository": repository,
        "tag": tag,
        "status": "queued",
        "bytes_done": 0,
        "bytes_total": 0,
        "percent": 0.0,
        "layers": 0,
        "current_status": None,
        "image_id": None,
        "error": None,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        snapshot = dict(job)
    _publish_job(EVENT_QUEUED, snapshot)

    threading.Thread(
        target=_run_pull, args=(job_id,), name=f"image-pull-{job_id}", daemon=True
    ).start()
    return snapshot


def _run_pull(job_id: str) -> None:
    job = get_pull(job_id)
    if job is None:
        return
    if job_id in _cancelled:
        finished = _set_job(job_id, status="cancelled", finished_at=_now())
        if finished:
            _publish_job(EVENT_CANCELLED, finished)
        _cancelled.discard(job_id)
        return

    started = _set_job(job_id, status="running", started_at=_now())
    if started:
        _publish_job(EVENT_STARTED, started)

    def _cancelled_now() -> bool:
        """Consulted per chunk, so a cancel lands at the next byte.

        Checking inside ``_progress`` instead delayed every cancel by up to
        one throttle interval, since that callback is the throttled one.
        """
        return job_id in _cancelled

    def _progress(snapshot: dict[str, Any]) -> None:
        updated = _set_job(
            job_id,
            bytes_done=int(snapshot.get("bytes_done") or 0),
            bytes_total=int(snapshot.get("bytes_total") or 0),
            percent=float(snapshot.get("percent") or 0.0),
            layers=int(snapshot.get("layers") or 0),
            current_status=snapshot.get("status"),
        )
        if updated:
            _publish_job(EVENT_PROGRESS, updated)

    try:
        result = _docker().pull_image(job["ref"], _progress, cancel=_cancelled_now)
    except PullCancelled:
        finished = _set_job(job_id, status="cancelled", finished_at=_now())
        if finished:
            _publish_job(EVENT_CANCELLED, finished)
        _cancelled.discard(job_id)
        return
    except BaseException as exc:  # noqa: BLE001 — surface any failure on the job
        if job_id in _cancelled:
            finished = _set_job(job_id, status="cancelled", finished_at=_now())
            if finished:
                _publish_job(EVENT_CANCELLED, finished)
        else:
            finished = _set_job(
                job_id,
                status="failed",
                error=str(exc) or type(exc).__name__,
                finished_at=_now(),
            )
            if finished:
                _publish_job(EVENT_FAILED, finished)
        _cancelled.discard(job_id)
        return

    finished = _set_job(
        job_id,
        status="completed",
        percent=100.0,
        bytes_done=int(result.get("bytes_done") or 0),
        bytes_total=int(result.get("bytes_total") or 0),
        image_id=result.get("id") or None,
        current_status=None,
        finished_at=_now(),
    )
    if finished:
        _publish_job(EVENT_COMPLETED, finished)
    _cancelled.discard(job_id)


def cancel_pull(job_id: str) -> dict[str, Any] | None:
    """Request cancellation of a queued or running pull."""
    job = get_pull(job_id)
    if job is None:
        return None
    if job.get("status") in TERMINAL_STATES:
        return job
    _cancelled.add(job_id)
    if job.get("status") == "queued":
        finished = _set_job(job_id, status="cancelled", finished_at=_now())
        if finished:
            _publish_job(EVENT_CANCELLED, finished)
        return finished
    return _set_job(job_id, cancel_requested=True)


def clear_finished_pulls() -> int:
    """Drop terminal jobs from the registry. Returns how many were removed."""
    with _jobs_lock:
        stale = [k for k, v in _jobs.items() if v.get("status") in TERMINAL_STATES]
        for k in stale:
            _jobs.pop(k, None)
    return len(stale)


# ── Deletion ─────────────────────────────────────────────────────────────────


ACTIVE_DEPLOYMENT_STATES = ("running", "pending", "starting", "pulling")


def images_in_use() -> dict[str, list[str]]:
    """Map image ref -> the deployments and containers holding it.

    Two sources, because neither alone is complete: the deployment records
    cover a deploy that is still pulling and has no container yet, and the
    managed containers cover clusters and anything reconciled from labels.
    """
    in_use: dict[str, list[str]] = {}
    from spark_pulse import tools

    def _claim(ref: str, holder: str) -> None:
        if ref and holder not in in_use.setdefault(ref, []):
            in_use[ref].append(holder)

    try:
        deployments = tools.deployments.list_deployments() or []
    except Exception:  # pragma: no cover - defensive
        deployments = []
    for dep in deployments:
        if dep.get("status") not in ACTIVE_DEPLOYMENT_STATES:
            continue
        _claim(str(dep.get("image_ref") or dep.get("image") or ""), str(dep.get("id")))

    try:
        containers = tools.docker.list_managed_containers() or []
    except Exception:  # pragma: no cover - Docker may be unreachable
        containers = []
    for container in containers:
        if container.status != "running":
            continue
        holder = container.metadata.deployment or container.name
        _claim(container.metadata.image or container.image, holder)
    return in_use


def delete_image(ref: str, force: bool = False) -> dict[str, Any]:
    """Delete a local image, refusing when something running references it."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("ref is required")
    users = images_in_use().get(ref, [])
    if users:
        raise ValueError(
            f"Image {ref} is in use by running deployment(s) or cluster(s): "
            f"{', '.join(users)}"
        )
    docker = _docker()
    info = docker.image_info(ref)
    if info is None:
        raise ValueError(f"Image not present locally: {ref}")
    if not docker.remove_image(ref, force=force):
        raise ValueError(f"Image not present locally: {ref}")
    result = {
        "deleted": ref,
        "image_id": info.get("id", ""),
        "freed_bytes": int(info.get("size_bytes") or 0),
    }
    publish_event(EVENT_DELETED, ref, result)
    return result


# ── Distribution ─────────────────────────────────────────────────────────────


def _make_ssh_client(ssh_user: str | None) -> SSHClient:
    """Build the SSH client used for distribution (overridable in tests)."""
    return OpenSSHClient(user=ssh_user or None, host_key_policy="strict")


def _node_services(
    client: SSHClient | None, services: Any | None = None
) -> Callable[[Any], Any]:
    """The resolver distribution reaches nodes through.

    Node-bound services, not raw ssh: the transport, the local/peer branch and
    the command building are then the ones every other remote operation uses,
    and simulation swaps them at the resolver.
    """
    if services is not None:
        return services
    from spark_pulse import tools

    return tools.node_service.NodeServices(ssh_client=client)


def _node_has(info: dict[str, Any] | None, digest: str, image_id: str) -> bool:
    """Whether a node already carries this exact content.

    The digest is the authority — that is the identity the deploy pins — and
    the image ID is the fallback for a node whose daemon reports no repo
    digest. A matching *tag* is never enough: same tag, different ID is the
    digest-drift case, and refreshing it is the point.
    """
    if info is None:
        return False
    if digest:
        for entry in info.get("repo_digests") or []:
            if str(entry).partition("@")[2] == digest:
                return True
    remote_id = str(info.get("id") or "")
    return bool(remote_id and image_id and remote_id == image_id)


def sync_to_nodes(
    ref: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = 3600,
    client: SSHClient | None = None,
    services: Any | None = None,
    digest: str = "",
) -> dict[str, Any]:
    """Seed ``ref`` into the control node's registry, then have nodes pull it.

    ``docker save | ssh docker load`` used to do this, and it was silently
    wrong: the round trip changed the digest and emptied ``RepoDigests``, so a
    node could not resolve the digest-pinned reference it was later handed. It
    is gone, with no fallback — a wrong answer is worse than no answer.

    What happens instead:

    1. The image is copied into the registry on this node, digest preserved
       and *verified* against what the index advertises.
    2. Each node pulls from that registry, **anonymously**. No registry
       credential is sent anywhere; it stays in this node's secrets.
    3. A node already holding that digest is skipped.

    Args:
        ref: The image reference as the control node knows it.
        nodes: Node addresses to seed.
        ssh_user: SSH login for the nodes.
        timeout: Per-operation timeout, in seconds.
        client: SSH transport override (tests, simulation).
        services: Node-service resolver override (tests, simulation).
        digest: The advertised digest, when the caller knows it.

    Returns:
        The seeded location, the per-node results, and each node's own
        composed pull reference.
    """
    ref = (ref or "").strip()
    if not nodes:
        raise ValueError("No nodes specified")
    docker = _docker()
    info = docker.image_info(ref)
    if info is None:
        raise ValueError(f"Image not present locally: {ref}")
    local_id = str(info.get("id") or "")
    repository, _ = split_ref(ref)
    advertised = (digest or "").strip() or local_digest(info, repository)

    from spark_pulse import tools

    # Fetch once, on the node that holds the credential.
    seeded = tools.registry.seed(ref, advertised, timeout=timeout)
    pull_ref = str(seeded["pull_ref"])
    seed_digest = str(seeded["digest"])

    resolve = _node_services(client, services)

    def _one(address: str) -> dict[str, Any]:
        started = time.monotonic()
        node = tools.node_service.node_for(address, ssh_user=ssh_user or "")

        def _result(ok: bool, skipped: bool, error: str | None) -> dict[str, Any]:
            payload = {
                "node": address,
                "pull_ref": pull_ref,
                "digest": seed_digest,
                "ok": ok,
                "skipped": skipped,
                "error": error[:500] if error else None,
                "duration_s": round(time.monotonic() - started, 2),
            }
            publish_event(
                EVENT_SYNCED,
                ref,
                {"phase": "node", **payload},
            )
            return payload

        try:
            service = resolve(node)
            if _node_has(service.image_info(pull_ref), seed_digest, local_id):
                return _result(True, True, None)
        except (SSHError, OSError, RuntimeError) as exc:
            return _result(False, False, str(exc))

        try:
            service.pull_image(pull_ref)
        except (SSHError, OSError, RuntimeError) as exc:
            return _result(False, False, str(exc))
        return _result(True, False, None)

    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        results = list(pool.map(_one, nodes))

    payload = {
        "ref": ref,
        "image_id": local_id,
        "registry_base": seeded["registry_base"],
        "repository": seeded["repository"],
        "digest": seed_digest,
        "pull_ref": pull_ref,
        "seeded_with": seeded["tool"],
        # Said out loud: a node pulls from this registry with no credential.
        "nodes_need_credentials": False,
        "results": results,
        "ok": all(r["ok"] for r in results),
    }
    publish_event(EVENT_SYNCED, ref, payload)
    return payload
