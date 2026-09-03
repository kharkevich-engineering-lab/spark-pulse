"""Mock Remote Docker service for simulation mode.

Mirrors the real remote_docker.py API exactly — same argument shapes, same
return types (:class:`ContainerInfo`, :class:`ExecResult`) — for testing
without real Docker or SSH access.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from spark_pulse.tools.docker import (
    ContainerInfo,
    ContainerMetadata,
    ExecResult,
    _labels_match,
)


@dataclass
class MockRemoteContainer:
    """Simulated remote Docker container."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    status: str = "running"
    image: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    host: str = ""
    _removed: bool = field(default=False, repr=False)

    @property
    def is_running(self) -> bool:
        return self.status == "running"


class MockRemoteDockerService:
    """Mock RemoteDockerService for simulation mode.

    Simulates container lifecycle on local and remote nodes.
    """

    def __init__(self, scenario: str = "default"):
        """Initialize mock remote Docker service.

        Args:
            scenario: Simulation scenario ("default", "failed", "partial").
        """
        self._containers: dict[str, MockRemoteContainer] = {}
        self._scenario = scenario
        self._executed_commands: list[dict[str, Any]] = []

    def run_container(
        self,
        host: str,
        image: str,
        name: str,
        env_vars: dict[str, str],
        docker_config: dict[str, Any],
        metadata: ContainerMetadata,
        **kwargs: Any,
    ) -> ContainerInfo:
        """Run container on local or remote node (mocked)."""
        if not metadata.image:
            metadata.image = image
        labels = metadata.to_labels()
        self._executed_commands.append(
            {
                "action": "run_container",
                "host": host,
                "image": image,
                "name": name,
                "env_vars": env_vars,
                "docker_config": docker_config,
                "labels": labels,
            }
        )

        container = MockRemoteContainer(
            name=name,
            image=image,
            labels=labels,
            host=host,
            status="running" if self._scenario != "failed" else "error",
        )
        self._containers[name] = container
        return ContainerInfo(
            id=container.id,
            name=name,
            status=container.status,
            image=image,
            metadata=metadata,
            labels=labels,
        )

    def stop_container(
        self,
        host: str,
        name: str,
        timeout: int = 10,
    ) -> bool:
        """Stop container on local or remote node (mocked)."""
        self._executed_commands.append(
            {
                "action": "stop_container",
                "host": host,
                "name": name,
                "timeout": timeout,
            }
        )

        if name in self._containers:
            self._containers[name].status = "exited"
            return True
        return False

    def exec_container(
        self,
        host: str,
        container: str,
        command: list[str],
        timeout: int = 30,
    ) -> ExecResult:
        """Execute command inside container (mocked)."""
        self._executed_commands.append(
            {
                "action": "exec_container",
                "host": host,
                "container": container,
                "command": command,
                "timeout": timeout,
            }
        )

        # Return mock responses based on command
        cmd_str = " ".join(command)
        if "ray status" in cmd_str:
            return ExecResult(returncode=0, stdout="Cluster is ready. OK")
        if "nvidia-smi" in cmd_str:
            return ExecResult(returncode=0, stdout="1")
        if cmd_str == "env":
            return ExecResult(returncode=0, stdout="NCCL_SOCKET_IFNAME=eth0")

        return ExecResult(returncode=0)

    def copy_to_container(
        self,
        host: str,
        container: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
    ) -> bool:
        """Copy a file into a container (mocked)."""
        self._executed_commands.append(
            {
                "action": "copy_to_container",
                "host": host,
                "container": container,
                "local_path": local_path,
                "remote_path": remote_path,
            }
        )
        return self._scenario != "failed"

    def get_container_status(
        self,
        host: str,
        name: str,
    ) -> dict[str, Any]:
        """Get container status (mocked)."""
        if name in self._containers:
            c = self._containers[name]
            return {
                "status": c.status,
                "running": c.is_running,
                "id": c.id,
                "state": {
                    "Running": c.is_running,
                    "Pid": 12345,
                    "StartedAt": datetime.now(timezone.utc).isoformat(),
                },
                "error": None,
            }
        return {
            "status": "missing",
            "running": False,
            "id": None,
            "state": {},
            "error": f"Container '{name}' not found",
        }

    def list_managed_containers(
        self,
        host: str,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """List containers matching labels (mocked)."""
        self._executed_commands.append(
            {
                "action": "list_managed_containers",
                "host": host,
                "labels": labels,
            }
        )

        results: list[ContainerInfo] = []
        for container in self._containers.values():
            if not _labels_match(container.labels, labels):
                continue
            results.append(
                ContainerInfo(
                    id=container.id,
                    name=container.name,
                    image=container.image,
                    status=container.status,
                    metadata=ContainerMetadata.from_labels(container.labels),
                    labels=container.labels,
                )
            )

        return results

    @property
    def executed_commands(self) -> list[dict[str, Any]]:
        """Return list of all executed commands."""
        return self._executed_commands.copy()

    def reset(self) -> None:
        """Clear executed commands history."""
        self._executed_commands.clear()

    def clear_containers(self) -> None:
        """Remove all simulated containers."""
        self._containers.clear()


# The simulation-mode name that ``spark_pulse.tools`` re-exports.
RemoteDockerService = MockRemoteDockerService
