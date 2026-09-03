"""Docker container lifecycle management via Docker SDK for Python.

Provides container creation, stopping, status checking, and label-based
discovery for Spark Pulse managed deployments.

All managed containers carry ``spark-pulse.*`` labels that serve as the
single source of truth — no external state database needed.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

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
    """Docker SDK wrapper for container lifecycle management."""

    def __init__(self, client: Any | None = None):
        """Initialize with an optional Docker client (for testing).

        Args:
            client: A docker.DockerClient instance. If None, creates one
                    from the default environment.
        """
        self._client = client
        self._import_error: Exception | None = None

    @property
    def client(self) -> Any:
        """Lazy-initialize Docker client."""
        if self._client is None:
            try:
                import docker

                self._client = docker.from_env()
            except Exception as exc:
                self._import_error = exc
                raise RuntimeError(
                    f"Docker daemon not available. Is Docker running? Error: {exc}"
                ) from exc
        return self._client

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

        # Build host config
        host_config = client.create_host_config(
            privileged=privileged,
            memory=self._gb_to_bytes(memory_limit_gb) if memory_limit_gb else None,
            memory_swap=self._calc_memory_swap(memory_limit_gb),
            pids_limit=pids_limit,
            shm_size=f"{shm_size_gb}g",
            ulimits=[
                docker.types.Ulimit(
                    name="nofile", soft=nofile_limit, hard=nofile_limit
                ),
            ],
            device_requests=(
                [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]
                if privileged
                else []
            ),
            cap_add=(["IPC_LOCK"] if not privileged else None),
            network_mode="host" if not port_mappings else None,
            volumes=volumes,
        )

        # Add port mappings if specified
        if port_mappings:
            host_config["port_bindings"] = {
                p.split(":")[0]: [{"HostPort": p.split(":")[1]}] for p in port_mappings
            }

        # Clear entrypoint if requested
        entrypoint = [] if entrypoint_clear else None

        try:
            container = client.containers.run(
                image,
                name=name,
                detach=detach,
                environment=env_vars,
                labels=labels,
                host_config=host_config,
                entrypoint=entrypoint,
                remove=True,
            )
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
        filters: dict[str, Any] = {"label": MANAGED_FILTER}
        for key, value in (labels or {}).items():
            filters[key] = value
        containers = client.containers.list(all=True, filters=filters)
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
            filters={
                "label": MANAGED_FILTER,
                DEPLOYMENT_LABEL: deployment,
            },
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
            filters={
                "label": MANAGED_FILTER,
                RECIPE_LABEL: recipe,
            },
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
