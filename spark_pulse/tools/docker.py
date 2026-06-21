"""Docker container lifecycle management via Docker SDK for Python.

Provides container creation, stopping, status checking, and label-based
discovery for Spark Pulse managed deployments.

All managed containers carry ``spark-pulse.*`` labels that serve as the
single source of truth — no external state database needed.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Data Models ──────────────────────────────────────────────────────────────

_LABEL_PREFIX = "spark-pulse."


@dataclass
class ContainerMetadata:
    """Self-describing metadata stored as Docker labels."""

    deployment: str
    recipe: str
    image: str
    mode: str = "solo"
    created_at: str | None = None
    memory_limit_gb: float | None = None
    shm_size_gb: float = 64
    privileged: bool = True

    def to_labels(self) -> dict[str, str]:
        """Serialize to Docker label dict (prefix: spark-pulse.)."""
        prefix = _LABEL_PREFIX
        return {
            f"{prefix}managed": "true",
            f"{prefix}deployment": self.deployment,
            f"{prefix}recipe": self.recipe,
            f"{prefix}image": self.image,
            f"{prefix}mode": self.mode,
            f"{prefix}created_at": self.created_at or "",
            f"{prefix}version": "1",
            f"{prefix}memory_limit_gb": (
                str(self.memory_limit_gb) if self.memory_limit_gb else ""
            ),
            f"{prefix}shm_size_gb": str(self.shm_size_gb),
            f"{prefix}privileged": "true" if self.privileged else "false",
        }

    @classmethod
    def from_labels(cls, labels: dict[str, str] | None) -> "ContainerMetadata":
        """Deserialize from Docker labels."""
        if labels is None:
            labels = {}
        prefix = _LABEL_PREFIX

        def _get(key: str, default: str = "") -> str:
            return labels.get(f"{prefix}{key}", default)

        mem = _get("memory_limit_gb")
        return cls(
            deployment=_get("deployment"),
            recipe=_get("recipe"),
            image=_get("image"),
            mode=_get("mode", "solo"),
            created_at=_get("created_at") or None,
            memory_limit_gb=float(mem) if mem else None,
            shm_size_gb=float(_get("shm_size_gb", "64")),
            privileged=_get("privileged", "true") == "true",
        )


@dataclass
class ContainerInfo:
    """Immutable snapshot of a managed container."""

    id: str
    name: str
    status: str  # running | stopped | missing
    image: str
    metadata: ContainerMetadata


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
                    "Docker daemon not available. Is Docker running? " f"Error: {exc}"
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
        labels[f"{_LABEL_PREFIX}name"] = name

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
        return ContainerInfo(
            id=container.id,
            name=container.name,
            status="running",
            image=image,
            metadata=metadata,
        )

    def stop_container(self, name: str) -> bool:
        """Stop and remove a container by name.

        Args:
            name: Container name.

        Returns:
            True if the container was stopped and removed.
        """
        import docker

        client = self.client
        try:
            container = client.containers.get(name)
            container.stop(timeout=30)
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
            Dict with keys: status, id, state, error (if any).
        """
        import docker

        client = self.client
        try:
            container = client.containers.get(name)
            return {
                "status": container.status,
                "id": container.id,
                "state": container.attrs.get("State", {}),
                "error": None,
            }
        except docker.errors.NotFound:
            return {
                "status": "missing",
                "id": None,
                "state": {},
                "error": f"Container '{name}' not found",
            }
        except Exception as exc:
            # Catch mock NotFound or other errors
            exc_str = str(exc)
            if "not found" in exc_str.lower():
                return {
                    "status": "missing",
                    "id": None,
                    "state": {},
                    "error": f"Container '{name}' not found",
                }
            raise
        except docker.errors.APIError as exc:
            return {
                "status": "error",
                "id": None,
                "state": {},
                "error": str(exc),
            }

    def exec_in_container(
        self,
        container: str | Any,
        command: str,
        daemon: bool = False,
    ) -> str | subprocess.Popen:
        """Execute command inside a running container.

        Args:
            container: Container name or Container object.
            command: Command to execute.
            daemon: If True, run in background and return Popen.

        Returns:
            Command output string, or Popen if daemon=True.
        """

        client = self.client
        if isinstance(container, str):
            container = client.containers.get(container)

        if daemon:
            proc = container.exec_run(command, demux=True)
            return proc
        else:
            output = container.exec_run(command, demux=True)
            return output.output.decode() if output.output else ""

    def list_managed_containers(self) -> list[ContainerInfo]:
        """Return all Spark Pulse managed containers via label filter.

        Returns:
            List of ContainerInfo for all managed containers.
        """

        client = self.client
        containers = client.containers.list(
            all=True,
            filters={"label": f"{_LABEL_PREFIX}managed=true"},
        )
        return [self._container_to_info(c) for c in containers]

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
                "label": f"{_LABEL_PREFIX}managed=true",
                f"{_LABEL_PREFIX}deployment": deployment,
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
                "label": f"{_LABEL_PREFIX}managed=true",
                f"{_LABEL_PREFIX}recipe": recipe,
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


def list_managed_containers() -> list[ContainerInfo]:
    """Convenience function to list all managed containers."""
    return _get_service().list_managed_containers()


def get_container_by_deployment(deployment: str) -> ContainerInfo | None:
    """Convenience function to find container by deployment name."""
    return _get_service().get_container_by_deployment(deployment)
