"""Mock Docker SDK client for simulation mode.

Simulates container lifecycle operations without requiring a Docker daemon.
Mirrors the real docker.py API exactly.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Import real types so isinstance checks work across mock/real boundary
from spark_pulse.tools.docker import (
    ContainerInfo,
    DockerService,
    split_ref,
)


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
    log_lines: list[str] = field(default_factory=list)
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

    def logs(self, tail: int | str = "all", **_kwargs: Any) -> bytes:
        """Simulate ``docker logs`` over the recorded lines."""
        lines = self.log_lines
        if isinstance(tail, int):
            lines = lines[-tail:]
        return ("\n".join(lines) + ("\n" if lines else "")).encode()

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
        self.log_lines.append(f"[mock] exec: {text}")
        output = f"{text.split()[-1] if text else ''}\n".encode()
        return MockExecResult(exit_code=0, output=(output, None) if demux else output)


@dataclass
class MockImage:
    """Simulated Docker image."""

    id: str = field(default_factory=lambda: "sha256:" + uuid.uuid4().hex * 2)
    tags: list[str] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)


# A few engine images the simulated host already has, plus one it has not.
_SEED_IMAGES: list[tuple[str, str, int, str]] = [
    (
        "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm:0.1.0",
        "sha256:" + "a1" * 32,
        26_843_545_600,
        "2026-08-20T10:00:00Z",
    ),
    (
        "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/sglang:0.1.0",
        "sha256:" + "b2" * 32,
        22_548_578_304,
        "2026-08-22T09:30:00Z",
    ),
]

# Layer plan used to simulate a pull: (layer id, bytes).
_MOCK_PULL_LAYERS = [
    ("d1a5f0c9", 8_589_934_592),
    ("e2b6a1d0", 10_737_418_240),
    ("f3c7b2e1", 7_516_192_768),
]
_MOCK_PULL_TICK = 0.05


class MockImagesManager:
    """Simulates ``docker.DockerClient.images``."""

    def __init__(self):
        self._images: dict[str, MockImage] = {}
        for ref, image_id, size, created in _SEED_IMAGES:
            self.add(ref, image_id=image_id, size=size, created=created)

    def add(
        self,
        ref: str,
        image_id: str | None = None,
        size: int = 1_073_741_824,
        created: str | None = None,
    ) -> MockImage:
        """Register an image as present on the simulated host."""
        repo, tag = split_ref(ref)
        image = MockImage(
            id=image_id or ("sha256:" + uuid.uuid4().hex * 2),
            tags=[f"{repo}:{tag}"] if not tag.startswith("sha256:") else [],
        )
        image.attrs = {
            "Id": image.id,
            "Size": size,
            "Created": created or datetime.now(timezone.utc).isoformat(),
            "RepoTags": list(image.tags),
            "RepoDigests": [f"{repo}@{image.id}"],
        }
        self._images[ref] = image
        if not tag.startswith("sha256:"):
            self._images[f"{repo}:{tag}"] = image
        return image

    def get(self, ref: str) -> MockImage:
        image = self._images.get(ref)
        if image is None:
            raise ImageNotFound(f"No such image: {ref}")
        return image

    def list(self, **_kwargs: Any) -> list[MockImage]:
        seen: dict[str, MockImage] = {}
        for image in self._images.values():
            seen[image.id] = image
        return list(seen.values())

    def remove(self, ref: str, force: bool = False, **_kwargs: Any) -> None:
        image = self._images.get(ref)
        if image is None:
            raise ImageNotFound(f"No such image: {ref}")
        for key in [k for k, v in self._images.items() if v is image]:
            del self._images[key]


class MockLowLevelAPI:
    """Simulates ``docker.DockerClient.api`` — only what pulls need."""

    def __init__(self, images: MockImagesManager):
        self._images = images

    def pull(
        self,
        repository: str,
        tag: str = "latest",
        stream: bool = True,
        decode: bool = True,
        **_kwargs: Any,
    ):
        """Yield layer status dicts the way the real streaming pull does."""
        ref = (
            f"{repository}@{tag}"
            if tag.startswith("sha256:")
            else f"{repository}:{tag}"
        )
        yield {"status": f"Pulling from {repository}", "id": tag}
        total = 0
        for layer_id, size in _MOCK_PULL_LAYERS:
            total += size
            for step in (1, 2, 3, 4):
                time.sleep(_MOCK_PULL_TICK)
                yield {
                    "status": "Downloading",
                    "id": layer_id,
                    "progressDetail": {
                        "current": int(size * step / 4),
                        "total": size,
                    },
                }
            yield {
                "status": "Pull complete",
                "id": layer_id,
                "progressDetail": {"current": size, "total": size},
            }
        self._images.add(ref, size=total)
        yield {"status": f"Status: Downloaded newer image for {ref}"}


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
        command: str | list[str] | None = None,
        **_kwargs: Any,
    ) -> MockContainer:
        """Simulate running a container."""
        container = MockContainer(
            id=uuid.uuid4().hex[:12],
            name=name,
            status="running",
            image=image,
            labels=labels or {},
            log_lines=[f"[mock] started {name} from {image}"],
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
            # Docker's ``label`` filter takes a list (a bare string is also
            # accepted): "key" matches presence, "key=value" matches a value.
            raw = filters.get("label")
            if raw is not None:
                terms = [raw] if isinstance(raw, str) else list(raw)
                for term in terms:
                    if "=" in term:
                        key, value = term.split("=", 1)
                        containers = [
                            c for c in containers if (c.labels or {}).get(key) == value
                        ]
                    else:
                        containers = [c for c in containers if term in (c.labels or {})]

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
        self.images = MockImagesManager()
        self.api = MockLowLevelAPI(self.images)

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
        **extra: Any,
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
            **extra,
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
        self,
        container: str,
        local_path: str,
        remote_path: str,
        timeout: int = 120,
    ) -> bool:
        """Pretend the file was copied into the container."""
        return True

    def pull_image(
        self,
        ref: str,
        progress: Any | None = None,
        interval: float = 0.0,
        cancel: Any | None = None,
        stall_timeout: float | None = None,
    ) -> dict[str, Any]:
        """Pull through the real aggregation code over the simulated stream.

        The interval defaults to 0 so the handful of simulated ticks all reach
        the caller instead of being throttled into a single event. The stall
        watchdog is kept on, driven by the same config value as production, so
        simulation exercises the watched path rather than a shortcut around it.
        """
        return super().pull_image(
            ref,
            progress,
            interval=interval,
            cancel=cancel,
            stall_timeout=stall_timeout,
        )


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


def image_exists(ref: str) -> bool:
    """Convenience function: is the image present on the simulated host?"""
    return _get_service().image_exists(ref)


def pull_image(ref: str, progress: Any | None = None) -> dict[str, Any]:
    """Convenience function to simulate an image pull."""
    return _get_service().pull_image(ref, progress)


def image_info(ref: str) -> dict[str, Any] | None:
    """Convenience function to inspect a simulated image."""
    return _get_service().image_info(ref)


def list_images() -> list[dict[str, Any]]:
    """Convenience function to list simulated images."""
    return _get_service().list_images()


def remove_image(ref: str, force: bool = False) -> bool:
    """Convenience function to remove a simulated image."""
    return _get_service().remove_image(ref, force=force)
