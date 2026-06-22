"""Mock Docker SDK client for simulation mode.

Simulates container lifecycle operations without requiring a Docker daemon.
Mirrors the real docker.py API exactly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Import real types so isinstance checks work across mock/real boundary
from spark_pulse.tools.docker import (
    ContainerInfo,
    ContainerMetadata,
    DockerService,
)


@dataclass
class MockContainer:
    """Simulated Docker container."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    status: str = "running"
    image: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    _removed: bool = field(default=False, repr=False)
    attrs: dict[str, Any] = field(
        default_factory=lambda: {
            "State": {
                "Status": "running",
                "Running": True,
                "Pid": 0,
                "StartedAt": datetime.now(timezone.utc).isoformat(),
            }
        }
    )

    def stop(self, timeout: int = 10) -> None:
        """Simulate stopping the container."""
        self.status = "exited"
        self.attrs["State"]["Status"] = "exited"
        self.attrs["State"]["Running"] = False

    def remove(self, force: bool = False) -> None:
        """Simulate removing the container."""
        self._removed = True
        self.status = "removed"


class MockContainersManager:
    """Simulates docker.DockerClient.containers."""

    def __init__(self):
        self._containers: dict[str, MockContainer] = {}

    def run(
        self,
        image: str,
        name: str,
        detach: bool = True,
        environment: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        host_config: dict[str, Any] | None = None,
        entrypoint: list[str] | None = None,
        remove: bool = True,
    ) -> MockContainer:
        """Simulate running a container."""
        container = MockContainer(
            id=uuid.uuid4().hex[:12],
            name=name,
            status="running",
            image=image,
            labels=labels or {},
        )
        container.attrs["Image"] = image
        self._containers[name] = container
        return container

    def get(self, name: str) -> MockContainer:
        """Get a container by name."""
        if name not in self._containers:
            raise NotFound(f"Container {name} not found")
        container = self._containers[name]
        if container._removed:
            raise NotFound(f"Container {name} not found")
        return container

    def list(
        self, all: bool = False, filters: dict[str, Any] | None = None
    ) -> list[MockContainer]:
        """List containers with optional label filters."""
        containers = list(self._containers.values())
        if not all:
            containers = [c for c in containers if c.status == "running"]

        if filters:
            # Handle "label" filter (Docker SDK format: "key=value" or just "key")
            if "label" in filters:
                label_filter = filters["label"]
                if "=" in label_filter:
                    label_key, label_val = label_filter.split("=", 1)
                    containers = [
                        c
                        for c in containers
                        if (c.labels or {}).get(label_key) == label_val
                    ]
                else:
                    containers = [
                        c for c in containers if label_filter in (c.labels or {})
                    ]
            # Handle specific label key:value filters
            for key, value in filters.items():
                if key.startswith("spark-pulse.") and key != "label":
                    containers = [
                        c for c in containers if (c.labels or {}).get(key) == str(value)
                    ]

        return containers

    def stop_and_remove(self, name: str) -> bool:
        """Stop and remove a container."""
        if name in self._containers:
            self._containers[name].status = "exited"
            del self._containers[name]
            return True
        return False

    def remove_container(self, name: str) -> bool:
        """Remove a container by name (for stop_container flow)."""
        if name in self._containers:
            del self._containers[name]
            return True
        return False


class MockDockerClient:
    """Simulates docker.DockerClient for testing without Docker daemon."""

    def __init__(self):
        self.containers = MockContainersManager()

    def create_host_config(
        self,
        privileged: bool = False,
        memory: int | None = None,
        memory_swap: int | None = None,
        pids_limit: int | None = None,
        shm_size: str | None = None,
        ulimits: list[Any] | None = None,
        device_requests: list[Any] | None = None,
        cap_add: list[str] | None = None,
        network_mode: str | None = None,
        volumes: dict[str, dict[str, str]] | None = None,
        port_bindings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Simulate creating a host config (returns dict for mock)."""
        return {
            "privileged": privileged,
            "memory": memory,
            "memory_swap": memory_swap,
            "pids_limit": pids_limit,
            "shm_size": shm_size,
            "ulimits": ulimits,
            "device_requests": device_requests,
            "cap_add": cap_add,
            "network_mode": network_mode,
            "volumes": volumes,
            "port_bindings": port_bindings,
        }


class NotFound(Exception):
    """Simulated docker.errors.NotFound."""

    pass


class APIError(Exception):
    """Simulated docker.errors.APIError."""

    pass


class ImageNotFound(Exception):
    """Simulated docker.errors.ImageNotFound."""

    pass


# ── Mock service ─────────────────────────────────────────────────────────────

_mock_client: MockDockerClient | None = None


def _get_mock_client() -> MockDockerClient:
    """Get or create the mock Docker client."""
    global _mock_client
    if _mock_client is None:
        _mock_client = MockDockerClient()
    return _mock_client


def reset_mock() -> None:
    """Reset the mock state (for testing)."""
    global _mock_client
    _mock_client = MockDockerClient()


def list_mock_containers() -> list[dict[str, Any]]:
    """List all mock containers as dicts."""
    client = _get_mock_client()
    return [
        {
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "image": c.image,
            "labels": c.labels,
        }
        for c in client.containers.list(all=True)
    ]


# ── Mock DockerService methods ───────────────────────────────────────────────


def _mock_run_container(self, **kwargs: Any) -> ContainerInfo:
    mock = self._client or _get_mock_client()
    image = kwargs.get("image", "")
    name = kwargs.get("name", "")
    env_vars = kwargs.get("env_vars") or {}
    metadata = kwargs.get("metadata") or ContainerMetadata()
    labels = metadata.to_labels()
    labels["spark-pulse.name"] = name

    container = mock.containers.run(
        image=image,
        name=name,
        detach=True,
        environment=env_vars,
        labels=labels,
        host_config=mock.create_host_config(privileged=True),
        entrypoint=[],
        remove=True,
    )

    metadata.created_at = datetime.now(timezone.utc).isoformat()
    return ContainerInfo(
        id=container.id,
        name=container.name,
        status="running",
        image=image,
        metadata=metadata,
    )


def _mock_stop_container(self, name: str) -> bool:
    mock = self._client or _get_mock_client()
    return mock.containers.stop_and_remove(name)


def _mock_get_container_status(self, name: str) -> dict[str, Any]:
    mock = self._client or _get_mock_client()
    try:
        container = mock.containers.get(name)
        return {
            "status": container.status,
            "id": container.id,
            "state": container.attrs.get("State", {}),
            "error": None,
        }
    except NotFound:
        return {
            "status": "missing",
            "id": None,
            "state": {},
            "error": f"Container '{name}' not found",
        }


def _mock_list_managed_containers(self) -> list[ContainerInfo]:
    mock = self._client or _get_mock_client()
    containers = mock.containers.list(all=True)
    results: list[ContainerInfo] = []
    for c in containers:
        labels = c.labels or {}
        if labels.get("spark-pulse.managed") == "true":
            meta = ContainerMetadata.from_labels(labels)
            results.append(
                ContainerInfo(
                    id=c.id,
                    name=c.name,
                    status=c.status,
                    image=c.image,
                    metadata=meta,
                )
            )
    return results


def _mock_get_container_by_deployment(self, deployment: str) -> ContainerInfo | None:
    mock = self._client or _get_mock_client()
    containers = mock.containers.list(
        all=True,
        filters={
            "label": "spark-pulse.managed=true",
            "spark-pulse.deployment": deployment,
        },
    )
    # Also check without label filter since mock might not track managed containers
    if not containers:
        containers = mock.containers.list(all=True)
    for c in containers:
        if (
            getattr(c, "name", "") == deployment
            or (c.labels or {}).get("spark-pulse.deployment") == deployment
        ):
            meta = ContainerMetadata.from_labels(c.labels)
            return ContainerInfo(
                id=c.id,
                name=c.name,
                status=c.status,
                image=c.image,
                metadata=meta,
            )
    return None


# ── Monkey-patch DockerService with mock implementations ──────────────────────

DockerService.run_container = _mock_run_container
DockerService.stop_container = _mock_stop_container
DockerService.get_container_status = _mock_get_container_status
DockerService.list_managed_containers = _mock_list_managed_containers
DockerService.get_container_by_deployment = _mock_get_container_by_deployment


# ── Module-level convenience functions (mirrors spark_pulse.tools.docker) ────

_service: DockerService | None = None


def _get_service() -> DockerService:
    """Get the global DockerService instance."""
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
