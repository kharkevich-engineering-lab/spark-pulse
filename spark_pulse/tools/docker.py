"""Docker container lifecycle management via Docker SDK for Python.

Provides container creation, stopping, status checking, and label-based
discovery for Spark Pulse managed deployments.

All managed containers carry ``spark-pulse.*`` labels that serve as the
single source of truth — no external state database needed.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from spark_pulse.config import config
from spark_pulse.tools.labels import (
    CLUSTER_LABEL,
    CREATED_AT_LABEL,
    DEPLOYMENT_LABEL,
    HEAD_IP_LABEL,
    IMAGE_LABEL,
    LABEL_PREFIX,
    MANAGED_FILTER,
    MANAGED_LABEL,
    MEMORY_LIMIT_LABEL,
    MODE_LABEL,
    NAME_LABEL,
    NODE_RANK_LABEL,
    PRIVILEGED_LABEL,
    RAY_ENABLED_LABEL,
    RECIPE_LABEL,
    ROLE_LABEL,
    SHM_SIZE_LABEL,
    VERSION_LABEL,
)

logger = logging.getLogger(__name__)

# ── Data Models ──────────────────────────────────────────────────────────────

# Kept for backwards compatibility with callers that imported the private name.
_LABEL_PREFIX = LABEL_PREFIX


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Result of a command executed inside a container.

    Shaped like ``SSHResult`` so local and remote execs are interchangeable.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Whether the command succeeded."""
        return self.returncode == 0


def _missing_status(name: str) -> dict[str, Any]:
    """Status dict for a container that does not exist."""
    return {
        "status": "missing",
        "running": False,
        "id": None,
        "state": {},
        "error": f"Container '{name}' not found",
    }


def _decode(raw: Any) -> str:
    """Decode docker exec output (bytes or str) to text."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode(errors="replace")
    return str(raw)


def _label_filter(labels: dict[str, str] | None = None) -> list[str]:
    """Build docker's ``label`` filter list.

    Docker accepts only its own filter keys, so extra label constraints belong
    inside the ``label`` list: ``key`` matches presence, ``key=value`` matches
    a value.
    """
    out = [MANAGED_FILTER]
    for key, value in (labels or {}).items():
        out.append(key if value == "" else f"{key}={value}")
    return out


def _labels_match(labels: dict[str, str], wanted: dict[str, str] | None) -> bool:
    """Whether ``labels`` satisfies every filter in ``wanted``.

    An empty filter value matches any container carrying that label key.
    """
    for key, value in (wanted or {}).items():
        if value == "":
            if key not in labels:
                return False
        elif labels.get(key) != value:
            return False
    return True


# ── Image reference helpers ──────────────────────────────────────────────────

# Pull progress is aggregated across layers and reported at most this often.
PULL_PROGRESS_INTERVAL = 1.0


class PullCancelled(RuntimeError):
    """A pull was asked to stop and did."""


class PullStalled(RuntimeError):
    """A pull produced no progress for longer than the watchdog allows."""


def _close_quietly(stream: Any) -> None:
    """Close a pull stream, ignoring whatever it says about it."""
    closer = getattr(stream, "close", None)
    if closer is None:
        return
    try:
        closer()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("closing the pull stream failed: %s", exc)


def _watched_chunks(stream: Any, stall_timeout: float) -> Iterator[Any]:
    """Yield the stream's chunks, giving up when it goes quiet.

    docker-py sets no timeout on a pull, so a registry that accepts the
    connection and then stops sending holds the calling thread forever. The
    stream is drained on a helper thread and handed over through a queue, which
    turns "no bytes" into a bounded ``queue.Empty`` the caller can act on: the
    stream is closed and :class:`PullStalled` raised, naming the stall.

    A non-positive ``stall_timeout`` disables the watchdog and iterates
    directly, which is what the fast in-memory fakes want.
    """
    if not stall_timeout or stall_timeout <= 0:
        yield from stream
        return

    chunks: queue.Queue[tuple[str, Any]] = queue.Queue()

    def _drain() -> None:
        try:
            for chunk in stream:
                chunks.put(("chunk", chunk))
            chunks.put(("done", None))
        except BaseException as exc:  # noqa: BLE001 — replayed on the caller
            chunks.put(("error", exc))

    reader = threading.Thread(target=_drain, name="docker-pull-reader", daemon=True)
    reader.start()
    try:
        while True:
            try:
                kind, payload = chunks.get(timeout=stall_timeout)
            except queue.Empty:
                raise PullStalled(f"no pull progress for {stall_timeout:g}s") from None
            if kind == "chunk":
                yield payload
            elif kind == "done":
                return
            else:
                raise payload
    finally:
        # Covers every exit — exhausted, stalled, cancelled or the caller
        # breaking out — so the socket never outlives the iteration.
        _close_quietly(stream)


def split_ref(ref: str) -> tuple[str, str]:
    """Split an image reference into ``(repository, tag_or_digest)``.

    ``repo@sha256:...`` keeps the digest as the tag, which is what the
    low-level ``api.pull`` wants; ``repo:tag`` splits on the last colon that is
    not part of a registry host:port; a bare repo defaults to ``latest``.
    """
    ref = (ref or "").strip()
    if not ref:
        return "", ""
    if "@" in ref:
        repo, _, digest = ref.partition("@")
        return repo, digest
    head, sep, tail = ref.rpartition(":")
    # A colon in the last path segment is a tag; one before a "/" is a port.
    if sep and "/" not in tail:
        return head, tail
    return ref, "latest"


def _pull_percent(layers: dict[str, dict[str, int]]) -> tuple[int, int, float]:
    """Aggregate per-layer download counters into (done, total, percent)."""
    done = sum(layer.get("current", 0) for layer in layers.values())
    total = sum(layer.get("total", 0) for layer in layers.values())
    percent = (done / total * 100.0) if total else 0.0
    return done, total, round(min(percent, 100.0), 2)


@dataclass
class ContainerMetadata:
    """Self-describing metadata stored as Docker labels.

    This is the single metadata shape for every container service — local
    (:class:`DockerService`), remote (``RemoteDockerService``) and mock.
    """

    deployment: str = ""
    recipe: str = ""
    image: str = ""
    mode: str = "solo"
    created_at: str | None = None
    memory_limit_gb: float | None = None
    shm_size_gb: float = 64
    privileged: bool = True
    # Cluster membership — empty for solo deployments.
    cluster: str = ""
    role: str = ""
    node_rank: int = 0
    head_ip: str = ""
    ray_enabled: bool = False

    def to_labels(self) -> dict[str, str]:
        """Serialize to Docker label dict (prefix: spark-pulse.)."""
        labels = {
            MANAGED_LABEL: "true",
            DEPLOYMENT_LABEL: self.deployment,
            RECIPE_LABEL: self.recipe,
            IMAGE_LABEL: self.image,
            MODE_LABEL: self.mode,
            CREATED_AT_LABEL: self.created_at or "",
            VERSION_LABEL: "1",
            MEMORY_LIMIT_LABEL: (
                str(self.memory_limit_gb) if self.memory_limit_gb else ""
            ),
            SHM_SIZE_LABEL: str(self.shm_size_gb),
            PRIVILEGED_LABEL: "true" if self.privileged else "false",
        }
        if self.cluster:
            labels[CLUSTER_LABEL] = self.cluster
            labels[ROLE_LABEL] = self.role
            labels[NODE_RANK_LABEL] = str(self.node_rank)
            labels[RAY_ENABLED_LABEL] = "true" if self.ray_enabled else "false"
            if self.head_ip:
                labels[HEAD_IP_LABEL] = self.head_ip
        return labels

    @classmethod
    def from_labels(cls, labels: dict[str, str] | None) -> ContainerMetadata:
        """Deserialize from Docker labels."""
        if labels is None:
            labels = {}

        def _get(key: str, default: str = "") -> str:
            return labels.get(f"{LABEL_PREFIX}{key}", default)

        mem = _get("memory_limit_gb")
        rank = _get("node_rank")
        return cls(
            deployment=_get("deployment"),
            recipe=_get("recipe"),
            image=_get("image"),
            mode=_get("mode", "solo"),
            created_at=_get("created_at") or None,
            memory_limit_gb=float(mem) if mem else None,
            shm_size_gb=float(_get("shm_size_gb", "64")),
            privileged=_get("privileged", "true") == "true",
            cluster=_get("cluster"),
            role=_get("role"),
            node_rank=int(rank) if rank.isdigit() else 0,
            head_ip=_get("head_ip"),
            ray_enabled=_get("ray_enabled") == "true",
        )


@dataclass
class ContainerInfo:
    """Immutable snapshot of a managed container.

    ``labels`` carries the raw Docker labels so consumers can read keys that
    :class:`ContainerMetadata` does not model; ``metadata`` is the parsed view.
    """

    id: str
    name: str
    status: str  # running | stopped | missing
    image: str
    metadata: ContainerMetadata = field(default_factory=lambda: ContainerMetadata())
    labels: dict[str, str] = field(default_factory=dict)


# ── Docker Service ───────────────────────────────────────────────────────────


class DockerService:
    """Docker SDK wrapper for container lifecycle management.

    One service instance is shared process-wide, but a ``docker.DockerClient``
    is **not** thread-safe: upstream documents that each thread needs its own,
    the same rule ``requests.Session`` carries, because the client is a session
    with a connection pool behind it. The health monitor, the image-pull
    threads, the readiness watcher, the distribution fan-outs and every request
    thread all reach this service, so the client is held in thread-local
    storage and created lazily per thread.

    An explicitly injected client is the exception: tests and
    :class:`~spark_pulse.mock.docker.MockDockerService` hand in a fake that is
    the whole point of the object, so it is returned to every thread unchanged.
    """

    def __init__(self, client: Any | None = None):
        """Initialize with an optional Docker client (for testing).

        Args:
            client: A docker.DockerClient instance. If given it is used by
                    every thread as-is. If None, each thread lazily creates
                    its own from the default environment.
        """
        self._injected_client = client
        self._local = threading.local()
        self._import_error: Exception | None = None

    @property
    def client(self) -> Any:
        """The Docker client for the calling thread."""
        if self._injected_client is not None:
            return self._injected_client
        existing = getattr(self._local, "client", None)
        if existing is not None:
            return existing
        try:
            import docker

            created = docker.from_env()
        except Exception as exc:
            self._import_error = exc
            raise RuntimeError(
                f"Docker daemon not available. Is Docker running? Error: {exc}"
            ) from exc
        self._local.client = created
        return created

    def run_container(
        self,
        image: str,
        name: str,
        env_vars: dict[str, str],
        metadata: ContainerMetadata,
        privileged: bool = True,
        memory_limit_gb: float | None = None,
        shm_size_gb: float = 64,
        pids_limit: int = 4096,
        nofile_limit: int = 1048576,
        cache_dirs: list[str] | None = None,
        port_mappings: list[str] | None = None,
        entrypoint_clear: bool = True,
        detach: bool = True,
        command: str | list[str] | None = None,
        mounts: dict[str, str] | None = None,
        network_host: bool | None = None,
        ipc_host: bool = False,
        devices: list[str] | None = None,
        cap_add: list[str] | None = None,
        ulimits: dict[str, str] | None = None,
        auto_remove: bool = True,
    ) -> ContainerInfo:
        """Build and start a container with spark-pulse labels.

        Args:
            image: Docker image to use.
            name: Container name.
            env_vars: Environment variables to set.
            metadata: Container metadata (stored as labels).
            privileged: Run in privileged mode.
            memory_limit_gb: Memory limit in GB.
            shm_size_gb: /dev/shm size in GB.
            pids_limit: Maximum number of PIDs in the container.
            nofile_limit: Maximum number of open files (ulimit nofile).
            cache_dirs: Host cache directories to mount.
            port_mappings: Port mappings like ["8000:8000"].
            entrypoint_clear: Clear the image entrypoint.
            detach: Run in detached mode.
            command: Container command — the native runtime starts an idle
                container (``sleep infinity``) and execs into it afterwards.
            mounts: Explicit ``host_path -> container_path`` bind mounts, on
                top of ``cache_dirs`` (which mount at the same path).
            network_host: Force host networking on/off. ``None`` keeps the
                legacy behaviour (host unless ports are published).
            ipc_host: Share the host IPC namespace.
            devices: Device paths to expose, e.g. ``/dev/infiniband``.
            cap_add: Extra capabilities (added to the non-privileged default).
            ulimits: Extra ulimits as ``{name: "soft[:hard]"}``.
            auto_remove: Remove the container when it exits. The native
                runtime turns this off so ``docker logs`` survives a crash.

        Returns:
            ContainerInfo for the created container.
        """
        import docker

        client = self.client
        labels = metadata.to_labels()
        labels[NAME_LABEL] = name

        # Build volume mounts for cache dirs
        volumes: dict[str, dict[str, str]] = {}
        if cache_dirs:
            for host_dir in cache_dirs:
                container_dir = host_dir  # mount at same path
                volumes[host_dir] = {
                    "bind": container_dir,
                    "mode": "rw",
                }
        for host_path, container_path in (mounts or {}).items():
            volumes[host_path] = {"bind": container_path, "mode": "rw"}

        limits = [
            docker.types.Ulimit(name="nofile", soft=nofile_limit, hard=nofile_limit)
        ]
        for name_, raw in (ulimits or {}).items():
            if name_ == "nofile":
                continue
            soft, _, hard = str(raw).partition(":")
            limits.append(
                docker.types.Ulimit(name=name_, soft=int(soft), hard=int(hard or soft))
            )

        if network_host is None:
            network_mode = "host" if not port_mappings else None
        else:
            network_mode = "host" if network_host else None

        extra_caps = list(cap_add or [])
        if not privileged and "IPC_LOCK" not in extra_caps:
            extra_caps.append("IPC_LOCK")

        # Clear entrypoint if requested
        entrypoint = [] if entrypoint_clear else None

        try:
            kwargs: dict[str, Any] = {
                "name": name,
                "detach": detach,
                "environment": env_vars,
                "labels": labels,
                "entrypoint": entrypoint,
                "remove": auto_remove,
                "privileged": privileged,
                "pids_limit": pids_limit,
                "shm_size": f"{shm_size_gb}g",
                "ulimits": limits,
                "device_requests": [
                    docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
                ],
                "volumes": volumes,
            }
            if memory_limit_gb:
                kwargs["mem_limit"] = self._gb_to_bytes(memory_limit_gb)
                memswap = self._calc_memory_swap(memory_limit_gb)
                if memswap is not None:
                    kwargs["memswap_limit"] = memswap
            if not privileged and extra_caps:
                kwargs["cap_add"] = extra_caps
            if network_mode:
                kwargs["network_mode"] = network_mode
            if ipc_host:
                kwargs["ipc_mode"] = "host"
            if devices:
                kwargs["devices"] = [f"{d}:{d}:rwm" for d in devices]
            if port_mappings:
                # "host:container" -> {container: host}
                kwargs["ports"] = {
                    p.split(":")[1]: int(p.split(":")[0]) for p in port_mappings
                }
            if command is not None:
                kwargs["command"] = command
            container = client.containers.run(image, **kwargs)
        except docker.errors.ImageNotFound:
            raise RuntimeError(f"Image not found: {image}")
        except docker.errors.APIError as exc:
            raise RuntimeError(f"Docker API error: {exc}") from exc

        metadata.created_at = datetime.now(timezone.utc).isoformat()
        labels[CREATED_AT_LABEL] = metadata.created_at
        return ContainerInfo(
            id=container.id,
            name=container.name,
            status="running",
            image=image,
            metadata=metadata,
            labels=labels,
        )

    # ── Images ─────────────────────────────────────────────────────────────

    def image_exists(self, ref: str) -> bool:
        """Whether the image reference resolves to an image on this host."""
        if not ref:
            return False
        try:
            self.client.images.get(ref)
            return True
        except Exception as exc:
            if "not found" in str(exc).lower() or "no such image" in str(exc).lower():
                return False
            logger.debug("image_exists(%s) failed: %s", ref, exc)
            return False

    def image_info(self, ref: str) -> dict[str, Any] | None:
        """Return ``{id, size_bytes, created, repo_tags, repo_digests}`` or None."""
        try:
            image = self.client.images.get(ref)
        except Exception:
            return None
        attrs = getattr(image, "attrs", None) or {}
        return {
            "id": getattr(image, "id", "") or attrs.get("Id", ""),
            "size_bytes": int(attrs.get("Size") or 0),
            "created": attrs.get("Created"),
            "repo_tags": list(attrs.get("RepoTags") or []),
            "repo_digests": list(attrs.get("RepoDigests") or []),
        }

    def pull_image(
        self,
        ref: str,
        progress: Any | None = None,
        interval: float = PULL_PROGRESS_INTERVAL,
        cancel: Callable[[], bool] | None = None,
        stall_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Pull ``ref``, reporting aggregated progress through ``progress``.

        The low-level API streams one status dict per layer chunk, which is far
        too chatty to forward as events. Layer counters are folded into a
        single ``{bytes_done, bytes_total, percent, layers}`` snapshot and the
        callback fires at most once per ``interval`` seconds, plus once at the
        end.

        Args:
            cancel: Consulted on **every** chunk, not on every snapshot, so a
                cancel is honoured at the next byte instead of waiting out the
                throttle interval. Returning True raises :class:`PullCancelled`.
            stall_timeout: Seconds of silence that fail the pull with
                :class:`PullStalled`. Defaults to
                ``config.docker_pull_stall_timeout_seconds``; pass 0 to disable.
        """
        if not ref:
            raise RuntimeError("pull_image needs an image reference")
        if stall_timeout is None:
            stall_timeout = float(config.docker_pull_stall_timeout_seconds)
        repo, tag = split_ref(ref)
        layers: dict[str, dict[str, int]] = {}
        # Seeded with "now" so the first snapshot waits a full interval rather
        # than firing on the very first chunk.
        last_emit = time.monotonic()
        last_status = ""

        def _emit(force: bool = False) -> None:
            nonlocal last_emit
            if progress is None:
                return
            now = time.monotonic()
            if not force and now - last_emit < interval:
                return
            last_emit = now
            done, total, percent = _pull_percent(layers)
            progress(
                {
                    "ref": ref,
                    "status": last_status,
                    "layers": len(layers),
                    "bytes_done": done,
                    "bytes_total": total,
                    "percent": percent,
                }
            )

        try:
            stream = self.client.api.pull(repo, tag=tag, stream=True, decode=True)
            for chunk in _watched_chunks(stream, stall_timeout):
                if cancel is not None and cancel():
                    raise PullCancelled(f"pull of {ref} cancelled")
                if not isinstance(chunk, dict):
                    continue
                error = chunk.get("error") or chunk.get("errorDetail")
                if error:
                    message = (
                        error.get("message") if isinstance(error, dict) else str(error)
                    )
                    raise RuntimeError(f"pull of {ref} failed: {message}")
                status = str(chunk.get("status") or "")
                if status:
                    last_status = status
                layer_id = str(chunk.get("id") or "")
                detail = chunk.get("progressDetail") or {}
                if layer_id and isinstance(detail, dict) and detail.get("total"):
                    entry = layers.setdefault(layer_id, {"current": 0, "total": 0})
                    entry["total"] = int(detail.get("total") or entry["total"])
                    if status.startswith("Download"):
                        entry["current"] = int(detail.get("current") or 0)
                    elif status in ("Pull complete", "Already exists"):
                        entry["current"] = entry["total"]
                elif layer_id and status in ("Pull complete", "Already exists"):
                    entry = layers.setdefault(layer_id, {"current": 0, "total": 0})
                    entry["current"] = entry["total"]
                _emit()
        except PullStalled as exc:
            raise PullStalled(f"pull of {ref} stalled: {exc}") from None
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"pull of {ref} failed: {exc}") from exc

        done, total, _ = _pull_percent(layers)
        last_status = "pull complete"
        for entry in layers.values():
            entry["current"] = entry["total"]
        _emit(force=True)
        info = self.image_info(ref) or {}
        return {
            "ref": ref,
            "repository": repo,
            "tag": tag,
            "bytes_done": max(done, 0),
            "bytes_total": max(total, 0),
            "percent": 100.0,
            "id": info.get("id", ""),
            "size_bytes": info.get("size_bytes", 0),
        }

    def remove_image(self, ref: str, force: bool = False) -> bool:
        """Remove a local image. Returns False when it was not present."""
        try:
            self.client.images.remove(ref, force=force)
            return True
        except Exception as exc:
            if "not found" in str(exc).lower() or "no such image" in str(exc).lower():
                return False
            raise RuntimeError(f"could not remove image {ref}: {exc}") from exc

    def list_images(self) -> list[dict[str, Any]]:
        """Return every local image as ``image_info``-shaped dicts."""
        try:
            images = self.client.images.list()
        except Exception as exc:
            raise RuntimeError(f"could not list images: {exc}") from exc
        out: list[dict[str, Any]] = []
        for image in images:
            attrs = getattr(image, "attrs", None) or {}
            out.append(
                {
                    "id": getattr(image, "id", "") or attrs.get("Id", ""),
                    "size_bytes": int(attrs.get("Size") or 0),
                    "created": attrs.get("Created"),
                    "repo_tags": list(attrs.get("RepoTags") or []),
                    "repo_digests": list(attrs.get("RepoDigests") or []),
                }
            )
        return out

    def stop_container(self, name: str, timeout: int = 30) -> bool:
        """Stop and remove a container by name.

        Args:
            name: Container name.
            timeout: Seconds to wait before killing the container.

        Returns:
            True if the container was stopped and removed.
        """
        import docker

        client = self.client
        try:
            container = client.containers.get(name)
            container.stop(timeout=timeout)
            container.remove(force=True)
            return True
        except docker.errors.NotFound:
            return False
        except Exception as exc:
            # Catch mock NotFound or other errors
            exc_str = str(exc)
            if "not found" in exc_str.lower():
                return False
            logger.error("Failed to stop container %s: %s", name, exc)
            return False

    def get_container_status(self, name: str) -> dict[str, Any]:
        """Return container state: running, stopped, missing.

        Args:
            name: Container name.

        Returns:
            Dict with keys: status, running, id, state, error (if any).
        """
        import docker

        client = self.client
        try:
            container = client.containers.get(name)
            return {
                "status": container.status,
                "running": container.status == "running",
                "id": container.id,
                "state": container.attrs.get("State", {}),
                "error": None,
            }
        except docker.errors.NotFound:
            return _missing_status(name)
        except docker.errors.APIError as exc:
            return {
                "status": "error",
                "running": False,
                "id": None,
                "state": {},
                "error": str(exc),
            }
        except Exception as exc:
            # Catch mock NotFound or other errors
            if "not found" in str(exc).lower():
                return _missing_status(name)
            raise

    def exec_in_container(
        self,
        container: str | Any,
        command: str | list[str],
        detach: bool = False,
    ) -> ExecResult:
        """Execute a command inside a running container.

        Args:
            container: Container name or Container object.
            command: Command to execute, as a string or argv list.
            detach: If True, run in the background and return immediately.

        Returns:
            ExecResult with returncode, stdout and stderr. A detached exec
            returns an empty successful result.
        """

        client = self.client
        if isinstance(container, str):
            container = client.containers.get(container)

        if isinstance(command, (list, tuple)):
            command = list(command)

        result = container.exec_run(command, demux=True, detach=detach)
        if detach:
            return ExecResult(returncode=0, stdout="", stderr="")

        output = getattr(result, "output", None)
        if isinstance(output, tuple):
            raw_out, raw_err = output
        else:
            raw_out, raw_err = output, None
        exit_code = getattr(result, "exit_code", 0)
        return ExecResult(
            returncode=int(exit_code or 0),
            stdout=_decode(raw_out),
            stderr=_decode(raw_err),
        )

    def get_logs(self, name: str, tail: int = 200) -> str:
        """Return the tail of a container's stdout/stderr.

        The native runtime redirects the serve script into PID 1's stdout, so
        this is the deployment log.
        """
        client = self.client
        try:
            container = client.containers.get(name)
        except Exception as exc:
            if "not found" in str(exc).lower():
                return f"Container '{name}' not found"
            raise
        raw = container.logs(tail=tail)
        return _decode(raw)

    def copy_to_container(
        self,
        container: str,
        local_path: str,
        remote_path: str,
    ) -> bool:
        """Copy a local file into a container via ``docker cp``.

        Args:
            container: Container name or ID.
            local_path: Path on the host.
            remote_path: Destination path inside the container.

        Returns:
            True when the copy succeeded.
        """
        proc = subprocess.run(
            ["docker", "cp", local_path, f"{container}:{remote_path}"],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            logger.error(
                "docker cp %s -> %s:%s failed: %s",
                local_path,
                container,
                remote_path,
                proc.stderr.strip(),
            )
        return proc.returncode == 0

    def list_managed_containers(
        self,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """Return all Spark Pulse managed containers via label filter.

        Args:
            labels: Extra label filters. A value of ``""`` matches any
                container carrying that label key.

        Returns:
            List of ContainerInfo for all managed containers.
        """

        client = self.client
        containers = client.containers.list(
            all=True, filters={"label": _label_filter(labels)}
        )
        infos = [self._container_to_info(c) for c in containers]
        return [i for i in infos if _labels_match(i.labels, labels)]

    def get_container_by_deployment(self, deployment: str) -> ContainerInfo | None:
        """Find container by deployment name (from labels).

        Args:
            deployment: Deployment name to search for.

        Returns:
            ContainerInfo if found, None otherwise.
        """

        client = self.client
        containers = client.containers.list(
            all=True,
            filters={"label": _label_filter({DEPLOYMENT_LABEL: deployment})},
        )
        if containers:
            return self._container_to_info(containers[0])
        return None

    def get_container_by_recipe(self, recipe: str) -> list[ContainerInfo]:
        """Find all containers for a given recipe.

        Args:
            recipe: Recipe name to search for.

        Returns:
            List of ContainerInfo matching the recipe.
        """

        client = self.client
        containers = client.containers.list(
            all=True,
            filters={"label": _label_filter({RECIPE_LABEL: recipe})},
        )
        return [self._container_to_info(c) for c in containers]

    # ── Helpers ────────────────────────────────────────────────────────────

    def _container_to_info(self, container: Any) -> ContainerInfo:
        """Convert a Docker Container object to ContainerInfo."""
        labels = container.labels or {}
        metadata = ContainerMetadata.from_labels(labels)
        # Handle both real Docker containers (image is object) and mocks (image is string)
        if isinstance(container.image, str):
            image = container.image
        else:
            image = (
                container.image.tags[0]
                if getattr(container.image, "tags", None)
                else container.image.id
            )
        return ContainerInfo(
            id=container.id,
            name=container.name,
            status=container.status,
            image=image,
            metadata=metadata,
            labels=dict(labels),
        )

    @staticmethod
    def _gb_to_bytes(gb: float) -> int:
        """Convert GB to bytes."""
        return int(gb * 1024 * 1024 * 1024)

    @staticmethod
    def _calc_memory_swap(memory_limit_gb: float | None) -> int | None:
        """Calculate memory-swap limit.

        If memory_limit_gb is set, memory-swap defaults to limit + 10GB.
        If None, no swap limit is set.
        """
        if memory_limit_gb is None:
            return None
        return int((memory_limit_gb + 10) * 1024 * 1024 * 1024)


# ── Module-level convenience functions ───────────────────────────────────────

_service: DockerService | None = None


def _get_service() -> DockerService:
    """Get or create the global DockerService instance."""
    global _service
    if _service is None:
        _service = DockerService()
    return _service


def run_container(**kwargs: Any) -> ContainerInfo:
    """Convenience function to run a container."""
    return _get_service().run_container(**kwargs)


def stop_container(name: str) -> bool:
    """Convenience function to stop a container."""
    return _get_service().stop_container(name)


def get_container_status(name: str) -> dict[str, Any]:
    """Convenience function to get container status."""
    return _get_service().get_container_status(name)


def list_managed_containers(
    labels: dict[str, str] | None = None,
) -> list[ContainerInfo]:
    """Convenience function to list all managed containers."""
    return _get_service().list_managed_containers(labels)


def get_container_by_deployment(deployment: str) -> ContainerInfo | None:
    """Convenience function to find container by deployment name."""
    return _get_service().get_container_by_deployment(deployment)


def image_exists(ref: str) -> bool:
    """Convenience function: is the image present on this host?"""
    return _get_service().image_exists(ref)


def pull_image(ref: str, progress: Any | None = None) -> dict[str, Any]:
    """Convenience function to pull an image with aggregated progress."""
    return _get_service().pull_image(ref, progress)


def image_info(ref: str) -> dict[str, Any] | None:
    """Convenience function to inspect a local image."""
    return _get_service().image_info(ref)


def list_images() -> list[dict[str, Any]]:
    """Convenience function to list local images."""
    return _get_service().list_images()


def remove_image(ref: str, force: bool = False) -> bool:
    """Convenience function to remove a local image."""
    return _get_service().remove_image(ref, force=force)
