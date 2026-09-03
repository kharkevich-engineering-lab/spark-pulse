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
    DockerService,
)
from spark_pulse.tools.labels import LABEL_PREFIX


@dataclass
class MockExecResult:
    """Simulated result of ``Container.exec_run``."""

    exit_code: int = 0
    output: Any = b""


@dataclass
class MockContainer:
    """Simulated Docker container."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    status: str = "running"
    image: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    executed_commands: list[str] = field(default_factory=list)
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

    def exec_run(
        self,
        command: str | list[str],
        demux: bool = False,
        detach: bool = False,
        **_kwargs: Any,
    ) -> "MockExecResult":
        """Simulate executing a command inside the container."""
        text = command if isinstance(command, str) else " ".join(command)
        self.executed_commands.append(text)
        output = f"{text.split()[-1] if text else ''}\n".encode()
        return MockExecResult(exit_code=0, output=(output, None) if demux else output)


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
        containers = [c for c in self._containers.values() if not c._removed]
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
            # Handle specific label key:value filters. An empty value means
            # "carries this label key", matching the real service's semantics.
            for key, value in filters.items():
                if key.startswith(LABEL_PREFIX) and key != "label":
                    if value == "":
                        containers = [c for c in containers if key in (c.labels or {})]
                    else:
                        containers = [
                            c
                            for c in containers
                            if (c.labels or {}).get(key) == str(value)
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


class APIError(Exception):
    """Simulated docker.errors.APIError."""


class ImageNotFound(Exception):
    """Simulated docker.errors.ImageNotFound."""


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
    global _mock_client, _service
    _mock_client = MockDockerClient()
    _service = None


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


# ── Mock DockerService ───────────────────────────────────────────────────────

# The real DockerService drives any duck-typed client, so the mock service is
# just the real one wired to MockDockerClient. No monkey-patching: the real
# implementation stays intact for the contract tests.


class MockDockerService(DockerService):
    """DockerService backed by the in-memory MockDockerClient."""

    def __init__(self, client: Any | None = None):
        super().__init__(client or _get_mock_client())

    def copy_to_container(
        self, container: str, local_path: str, remote_path: str
    ) -> bool:
        """Pretend the file was copied into the container."""
        return True


# ── Module-level convenience functions (mirrors spark_pulse.tools.docker) ────

_service: MockDockerService | None = None


def _get_service() -> MockDockerService:
    """Get the global mock DockerService instance."""
    global _service
    if _service is None:
        _service = MockDockerService()
    return _service


def run_container(**kwargs: Any) -> ContainerInfo:
    """Convenience function to run a container."""
    return _get_service().run_container(**kwargs)


def stop_container(name: str, timeout: int = 30) -> bool:
    """Convenience function to stop a container."""
    return _get_service().stop_container(name, timeout=timeout)


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
