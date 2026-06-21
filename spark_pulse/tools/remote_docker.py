"""Remote Docker service for container operations on local and remote nodes.

Provides a unified interface for running, stopping, and managing Docker
containers on both local and remote nodes via SSH.

ClusterOrchestrator depends on this abstraction, not raw SSH+Docker.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional

from spark_pulse.tools.ssh import SSHClient, OpenSSHClient, SSHResult
from spark_pulse.tools.docker import (
    DockerService,
    ContainerInfo,
    ContainerMetadata,
    _LABEL_PREFIX,
)

logger = logging.getLogger(__name__)


class RemoteDockerService:
    """Docker operations on local or remote nodes.

    Local node: uses DockerService (Phase 1) directly.
    Remote node: uses SSHClient + docker CLI.

    ClusterOrchestrator depends on this abstraction, not raw SSH+Docker.
    """

    def __init__(
        self,
        ssh_client: SSHClient | None = None,
    ):
        """Initialize remote Docker service.

        Args:
            ssh_client: SSH transport for remote operations. Defaults to OpenSSHClient.
        """
        self._local = DockerService()
        self._ssh = ssh_client or OpenSSHClient()

    def run_container(
        self,
        host: str,
        image: str,
        name: str,
        env_vars: dict[str, str],
        docker_config: dict,
        labels: dict[str, str],
        **kwargs,
    ) -> str:
        """Run container on local or remote node.

        Args:
            host: Local (empty string) or remote host IP.
            image: Docker image name.
            name: Container name.
            env_vars: Environment variables.
            docker_config: Docker configuration (memory, shm, privileged, etc.).
            labels: Docker labels.
            **kwargs: Additional docker run arguments.

        Returns:
            Container ID.
        """
        if not host:
            return self._local.run_container(
                image, name, env_vars, docker_config, labels, **kwargs
            )

        # Remote node — build docker run command via SSH
        container_id = self._run_remote(
            host, image, name, env_vars, docker_config, labels, **kwargs
        )
        return container_id

    def _run_remote(
        self,
        host: str,
        image: str,
        name: str,
        env_vars: dict[str, str],
        docker_config: dict,
        labels: dict[str, str],
        **kwargs,
    ) -> str:
        """Build and execute docker run command on remote node."""
        cmd_parts = ["docker", "run", "-d"]

        # Environment variables
        for key, value in env_vars.items():
            cmd_parts.extend(["-e", f"{key}={value}"])

        # Docker config
        if docker_config.get("privileged"):
            cmd_parts.append("--privileged")
        if docker_config.get("memory_limit_gb"):
            cmd_parts.extend(["--memory", f"{docker_config['memory_limit_gb']}g"])
        if docker_config.get("memory_swap_limit_gb"):
            cmd_parts.extend([
                "--memory-swap",
                f"{docker_config['memory_swap_limit_gb']}g",
            ])
        if docker_config.get("pids_limit"):
            cmd_parts.extend(["--pids-limit", str(docker_config["pids_limit"])])
        if docker_config.get("shm_size_gb"):
            cmd_parts.extend(["--shm-size", f"{docker_config['shm_size_gb']}g"])
        if docker_config.get("nofile_limit"):
            cmd_parts.extend(["--ulimit", f"nofile={docker_config['nofile_limit']}"])

        # Cache dir mounts
        for cache_dir in docker_config.get("cache_dirs", []):
            cmd_parts.extend(["-v", f"{cache_dir}:{cache_dir}:rw"])

        # GPU device request
        if docker_config.get("gpu_count", 0) > 0:
            cmd_parts.extend(["--gpus", "all"])

        # Labels
        for key, value in labels.items():
            cmd_parts.extend(["--label", f"{key}={value}"])

        # Container name
        cmd_parts.extend(["--name", name])

        # Additional kwargs
        if kwargs.get("entrypoint") is not None:
            cmd_parts.extend(["--entrypoint", kwargs["entrypoint"]])
        if kwargs.get("network"):
            cmd_parts.extend(["--network", kwargs["network"]])

        # Image
        cmd_parts.append(image)

        command = " ".join(cmd_parts)
        result = self._ssh.exec(host, command, timeout=120)

        if not result.ok:
            raise RuntimeError(f"docker run failed on {host}: {result.stderr}")

        # Return first line of stdout (container ID)
        return result.stdout.strip().split("\n")[0]

    def stop_container(
        self,
        host: str,
        name: str,
        timeout: int = 10,
    ) -> None:
        """Stop container on local or remote node.

        Args:
            host: Local (empty string) or remote host IP.
            name: Container name.
            timeout: Seconds to wait before killing.
        """
        if not host:
            self._local.stop_container(name, timeout)
            return

        result = self._ssh.exec(
            host, f"docker stop -t {timeout} {name}", timeout=timeout + 10
        )
        if not result.ok:
            logger.warning("Failed to stop container %s on %s: %s", name, host, result.stderr)

    def exec_container(
        self,
        host: str,
        container: str,
        command: list[str],
        timeout: int = 30,
    ) -> SSHResult:
        """Execute command inside container on local or remote node.

        Args:
            host: Local (empty string) or remote host IP.
            container: Container name or ID.
            command: Command to execute.
            timeout: Seconds before killing.

        Returns:
            SSHResult with command output.
        """
        if not host:
            # Local — use Docker SDK
            return self._local.exec_in_container(container, command, timeout)

        # Remote — docker exec via SSH
        cmd_str = " ".join(command)
        return self._ssh.exec(
            host, f"docker exec {container} {cmd_str}", timeout=timeout
        )

    def get_container_status(
        self,
        host: str,
        name: str,
    ) -> dict[str, Any]:
        """Get container status on local or remote node.

        Args:
            host: Local (empty string) or remote host IP.
            name: Container name.

        Returns:
            Dict with status, running, pid, etc.
        """
        if not host:
            return self._local.get_container_status(name)

        result = self._ssh.exec(
            host,
            f"docker inspect --format '{{{{json .State}}}}' {name}",
            timeout=10,
        )
        if not result.ok:
            return {"status": "not_found", "running": False}

        try:
            state = json.loads(result.stdout.strip())
            return {
                "status": "running" if state.get("Running") else "stopped",
                "running": state.get("Running", False),
                "pid": state.get("Pid", 0),
                "started_at": state.get("StartedAt", ""),
            }
        except json.JSONDecodeError:
            return {"status": "unknown", "running": False}

    def list_managed_containers(
        self,
        host: str,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """List containers matching labels on local or remote node.

        Args:
            host: Local (empty string) or remote host IP.
            labels: Label filters.

        Returns:
            List of ContainerInfo objects.
        """
        if not host:
            return self._local.list_managed_containers(labels)

        # Remote — docker ps --filter via SSH
        filter_args = ""
        if labels:
            for key, value in labels.items():
                filter_args += f' --filter "label={key}={value}"'

        result = self._ssh.exec(
            host,
            f"docker ps --filter label=spark-pulse.managed=true --format '{{{{json .}}}}'{filter_args}",
            timeout=10,
        )

        if not result.ok:
            return []

        containers = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                info = json.loads(line.strip())
                containers.append(ContainerInfo(
                    container_id=info.get("ID", ""),
                    name=info.get("Names", ""),
                    image=info.get("Image", ""),
                    status=info.get("Status", ""),
                    labels=labels or {},
                ))
            except json.JSONDecodeError:
                continue

        return containers
