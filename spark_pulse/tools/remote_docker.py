"""Remote Docker service for container operations on local and remote nodes.

Provides a unified interface for running, stopping, and managing Docker
containers on both local and remote nodes via SSH.

An empty ``host`` means "this node" and is served by :class:`DockerService`
through the Docker SDK; any other host goes over SSH to the docker CLI. Both
paths return the same shapes — :class:`ContainerInfo` and :class:`ExecResult` —
so callers never need to know which one they got.

ClusterOrchestrator depends on this abstraction, not raw SSH+Docker.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from spark_pulse.tools.docker import (
    ContainerInfo,
    ContainerMetadata,
    DockerService,
    ExecResult,
    _labels_match,
)
from spark_pulse.tools.labels import MANAGED_FILTER
from spark_pulse.tools.ssh import OpenSSHClient, SSHClient, SSHResult

logger = logging.getLogger(__name__)


def _to_exec_result(result: Any) -> ExecResult:
    """Normalise an SSHResult (or ExecResult) into an ExecResult."""
    if isinstance(result, ExecResult):
        return result
    return ExecResult(
        returncode=getattr(result, "returncode", 1),
        stdout=getattr(result, "stdout", "") or "",
        stderr=getattr(result, "stderr", "") or "",
    )


class RemoteDockerService:
    """Docker operations on local or remote nodes.

    Local node: uses :class:`DockerService` (Docker SDK) directly.
    Remote node: uses :class:`SSHClient` + the docker CLI.

    ClusterOrchestrator depends on this abstraction, not raw SSH+Docker.
    """

    def __init__(
        self,
        ssh_client: SSHClient | None = None,
        docker_service: DockerService | None = None,
    ):
        """Initialize remote Docker service.

        Args:
            ssh_client: SSH transport for remote operations. Defaults to OpenSSHClient.
            docker_service: Local Docker service. Defaults to DockerService().
        """
        self._local = docker_service or DockerService()
        self._ssh = ssh_client or OpenSSHClient()

    # ── Lifecycle ────────────────────────────────────────────────────────

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
        """Run a container on the local or a remote node.

        Args:
            host: Local (empty string) or remote host IP.
            image: Docker image name.
            name: Container name.
            env_vars: Environment variables.
            docker_config: Docker configuration (memory, shm, privileged, ...).
            metadata: Container metadata, serialized to spark-pulse labels.
            **kwargs: Additional docker run arguments (entrypoint, network).

        Returns:
            ContainerInfo for the started container.
        """
        docker_config = docker_config or {}
        if not metadata.image:
            metadata.image = image

        if not host:
            return self._local.run_container(
                image=image,
                name=name,
                env_vars=env_vars,
                metadata=metadata,
                privileged=bool(docker_config.get("privileged", True)),
                memory_limit_gb=docker_config.get("memory_limit_gb"),
                shm_size_gb=docker_config.get("shm_size_gb", 64),
                pids_limit=docker_config.get("pids_limit", 4096),
                nofile_limit=docker_config.get("nofile_limit", 1048576),
                cache_dirs=docker_config.get("cache_dirs"),
                port_mappings=docker_config.get("port_mappings"),
            )

        return self._run_remote(
            host, image, name, env_vars, docker_config, metadata, **kwargs
        )

    def _run_remote(
        self,
        host: str,
        image: str,
        name: str,
        env_vars: dict[str, str],
        docker_config: dict[str, Any],
        metadata: ContainerMetadata,
        **kwargs: Any,
    ) -> ContainerInfo:
        """Build and execute a docker run command on a remote node."""
        labels = metadata.to_labels()
        cmd_parts = ["docker", "run", "-d"]

        # Environment variables
        for key, value in env_vars.items():
            cmd_parts.extend(["-e", shlex.quote(f"{key}={value}")])

        # Docker config
        if docker_config.get("privileged"):
            cmd_parts.append("--privileged")
        if docker_config.get("memory_limit_gb"):
            cmd_parts.extend(["--memory", f"{docker_config['memory_limit_gb']}g"])
        if docker_config.get("memory_swap_limit_gb"):
            cmd_parts.extend(
                ["--memory-swap", f"{docker_config['memory_swap_limit_gb']}g"]
            )
        if docker_config.get("pids_limit"):
            cmd_parts.extend(["--pids-limit", str(docker_config["pids_limit"])])
        if docker_config.get("shm_size_gb"):
            cmd_parts.extend(["--shm-size", f"{docker_config['shm_size_gb']}g"])
        if docker_config.get("nofile_limit"):
            cmd_parts.extend(["--ulimit", f"nofile={docker_config['nofile_limit']}"])

        # Cache dir mounts
        for cache_dir in docker_config.get("cache_dirs", []) or []:
            cmd_parts.extend(["-v", f"{cache_dir}:{cache_dir}:rw"])

        # GPU device request
        if docker_config.get("gpu_count", 0) > 0:
            cmd_parts.extend(["--gpus", "all"])

        # Labels
        for key, value in labels.items():
            cmd_parts.extend(["--label", shlex.quote(f"{key}={value}")])

        # Container name
        cmd_parts.extend(["--name", name])

        # Additional kwargs
        if kwargs.get("entrypoint") is not None:
            cmd_parts.extend(["--entrypoint", shlex.quote(kwargs["entrypoint"])])
        if kwargs.get("network"):
            cmd_parts.extend(["--network", kwargs["network"]])

        # Image
        cmd_parts.append(image)

        result = self._ssh.exec(host, " ".join(cmd_parts), timeout=120)
        if not result.ok:
            raise RuntimeError(f"docker run failed on {host}: {result.stderr}")

        container_id = result.stdout.strip().split("\n")[0]
        return ContainerInfo(
            id=container_id,
            name=name,
            status="running",
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
        """Stop a container on the local or a remote node.

        Args:
            host: Local (empty string) or remote host IP.
            name: Container name.
            timeout: Seconds to wait before killing.

        Returns:
            True when the container was stopped.
        """
        if not host:
            return self._local.stop_container(name, timeout=timeout)

        result = self._ssh.exec(
            host, f"docker stop -t {timeout} {name}", timeout=timeout + 10
        )
        if not result.ok:
            logger.warning(
                "Failed to stop container %s on %s: %s", name, host, result.stderr
            )
        return bool(result.ok)

    # ── Exec / copy ──────────────────────────────────────────────────────

    def exec_container(
        self,
        host: str,
        container: str,
        command: list[str],
        timeout: int = 30,
    ) -> ExecResult:
        """Execute a command inside a container on the local or a remote node.

        Args:
            host: Local (empty string) or remote host IP.
            container: Container name or ID.
            command: Command argv.
            timeout: Seconds before killing.

        Returns:
            ExecResult with the command output.
        """
        if not host:
            return _to_exec_result(self._local.exec_in_container(container, command))

        cmd_str = " ".join(shlex.quote(part) for part in command)
        return _to_exec_result(
            self._ssh.exec(host, f"docker exec {container} {cmd_str}", timeout=timeout)
        )

    def copy_to_container(
        self,
        host: str,
        container: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
    ) -> bool:
        """Copy a local file into a container on the local or a remote node.

        Remote copies stage the file in ``/tmp`` on the node first, then
        ``docker cp`` it into the container.

        Args:
            host: Local (empty string) or remote host IP.
            container: Container name or ID.
            local_path: Path on this machine.
            remote_path: Destination path inside the container.
            timeout: Seconds before killing.

        Returns:
            True when the copy succeeded.
        """
        if not host:
            return self._local.copy_to_container(container, local_path, remote_path)

        staged = f"/tmp/spark-pulse-{container}-{local_path.rsplit('/', 1)[-1]}"
        try:
            self._ssh.copy(local_path, host, staged, timeout=timeout)
        except Exception as exc:
            logger.error("Failed to stage %s on %s: %s", local_path, host, exc)
            return False

        result = self._ssh.exec(
            host,
            f"docker cp {shlex.quote(staged)} "
            f"{shlex.quote(container)}:{shlex.quote(remote_path)} "
            f"&& rm -f {shlex.quote(staged)}",
            timeout=timeout,
        )
        if not result.ok:
            logger.error(
                "docker cp into %s on %s failed: %s", container, host, result.stderr
            )
        return bool(result.ok)

    # ── Inspection ───────────────────────────────────────────────────────

    def get_container_status(
        self,
        host: str,
        name: str,
    ) -> dict[str, Any]:
        """Get container status on the local or a remote node.

        Args:
            host: Local (empty string) or remote host IP.
            name: Container name.

        Returns:
            Dict with status, running, id, state, error.
        """
        if not host:
            return self._local.get_container_status(name)

        result = self._ssh.exec(
            host,
            f"docker inspect --format '{{{{json .State}}}}' {name}",
            timeout=10,
        )
        if not result.ok:
            return {
                "status": "missing",
                "running": False,
                "id": None,
                "state": {},
                "error": f"Container '{name}' not found on {host}",
            }

        try:
            state = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return {
                "status": "unknown",
                "running": False,
                "id": None,
                "state": {},
                "error": f"Could not parse docker inspect output for '{name}'",
            }

        running = bool(state.get("Running", False))
        return {
            "status": "running" if running else "stopped",
            "running": running,
            "id": None,
            "state": state,
            "error": None,
        }

    def list_managed_containers(
        self,
        host: str,
        labels: dict[str, str] | None = None,
    ) -> list[ContainerInfo]:
        """List managed containers matching labels on the local or a remote node.

        Args:
            host: Local (empty string) or remote host IP.
            labels: Extra label filters. An empty value matches any container
                carrying that label key.

        Returns:
            List of ContainerInfo objects.
        """
        if not host:
            return self._local.list_managed_containers(labels)

        filter_args = f" --filter label={MANAGED_FILTER}"
        for key, value in (labels or {}).items():
            filter_args += f" --filter label={key}" + (f"={value}" if value else "")

        result = self._ssh.exec(
            host,
            f"docker ps --all{filter_args} --format '{{{{json .}}}}'",
            timeout=10,
        )
        if not result.ok:
            return []

        containers: list[ContainerInfo] = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                info = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            container_labels = _parse_cli_labels(info.get("Labels", ""))
            if not _labels_match(container_labels, labels):
                continue
            containers.append(
                ContainerInfo(
                    id=info.get("ID", ""),
                    name=info.get("Names", ""),
                    status=_normalize_cli_status(info.get("State", info.get("Status"))),
                    image=info.get("Image", ""),
                    metadata=ContainerMetadata.from_labels(container_labels),
                    labels=container_labels,
                )
            )

        return containers


def _parse_cli_labels(raw: str) -> dict[str, str]:
    """Parse the comma-separated ``key=value`` label list from ``docker ps``."""
    labels: dict[str, str] = {}
    for pair in (raw or "").split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            labels[key.strip()] = value.strip()
    return labels


def _normalize_cli_status(raw: str | None) -> str:
    """Map a ``docker ps`` status/state string onto running | stopped."""
    text = (raw or "").lower()
    return "running" if "running" in text or text.startswith("up") else "stopped"


__all__ = ["RemoteDockerService", "SSHResult"]
