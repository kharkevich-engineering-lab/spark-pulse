"""How the process holds its Docker clients and its worker threads.

Two ceilings that used to be invisible:

* The docker router built a brand-new ``DockerService`` per request, and so a
  brand-new connection pool, thrown away at the end of the call.
* AnyIO's worker-thread limiter defaults to 40 and says so nowhere, so
  exhaustion — a handful of blocking Docker or SSH calls is enough — looked
  like the API going quiet for no reason.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import anyio.to_thread
import pytest

from spark_pulse.app import configure_thread_pool
from spark_pulse.config import config
from spark_pulse.routers import docker as docker_router
from spark_pulse.tools.docker import ContainerMetadata

# pytest-env forces SIMULATION_MODE=1, so ``spark_pulse.tools.docker`` as an
# attribute of the package is the mock re-export. The router holds the real
# module's ``_get_service``, so that is the singleton to reach for here.
real_docker = importlib.import_module("spark_pulse.tools.docker")

IMAGE = "ghcr.io/example/engine:latest"


def _fake_client() -> MagicMock:
    """A MagicMock shaped like ``docker.DockerClient`` over a dict."""
    client = MagicMock(name="DockerClient")
    store: dict[str, MagicMock] = {}

    def _run(image, name, labels=None, **_kwargs):
        container = MagicMock(name=f"Container({name})")
        container.id = f"id-{name}"
        container.name = name
        container.status = "running"
        container.image = image
        container.labels = dict(labels or {})
        container.attrs = {"State": {"Status": "running", "Running": True}}
        exec_result = MagicMock()
        exec_result.exit_code = 0
        exec_result.output = (b"hygiene-ok\n", None)
        container.exec_run.return_value = exec_result
        store[name] = container
        return container

    def _get(name):
        if name not in store:
            raise LookupError(f"Container {name} not found")
        return store[name]

    client.containers.run.side_effect = _run
    client.containers.get.side_effect = _get
    return client


@pytest.fixture
def counted_service(monkeypatch):
    """Count how many DockerServices the process builds, and reset the cache."""
    built: list[real_docker.DockerService] = []
    original_init = real_docker.DockerService.__init__

    def _counting_init(self, client=None):
        built.append(self)
        original_init(self, client=client if client is not None else _fake_client())

    monkeypatch.setattr(real_docker.DockerService, "__init__", _counting_init)
    monkeypatch.setattr(real_docker, "_service", None)
    return built


class TestRouterReusesOneService:
    """One service for the process, not one per request."""

    def test_the_exec_endpoint_does_not_build_a_service_per_call(
        self, counted_service, monkeypatch
    ):
        """Three requests, one connection pool."""
        monkeypatch.setattr(docker_router, "is_simulation", lambda: False)

        service = docker_router._service()
        service.run_container(
            image=IMAGE,
            name="hygiene-exec",
            env_vars={},
            metadata=ContainerMetadata(deployment="hygiene-exec", image=IMAGE),
        )

        for _ in range(3):
            result = docker_router.exec_in_deployment(
                "hygiene-exec", {"command": "echo hygiene-ok"}
            )
            assert result["status"] == "success"
            assert "hygiene-ok" in result["output"]

        assert len(counted_service) == 1

    def test_the_logs_endpoint_uses_the_same_service(
        self, counted_service, monkeypatch
    ):
        """Two different endpoints, still the one service."""
        monkeypatch.setattr(docker_router, "is_simulation", lambda: False)

        service = docker_router._service()
        service.run_container(
            image=IMAGE,
            name="hygiene-logs",
            env_vars={},
            metadata=ContainerMetadata(deployment="hygiene-logs", image=IMAGE),
        )
        service.client.containers.get("hygiene-logs").logs.return_value = b"line\n"

        docker_router.exec_in_deployment("hygiene-logs", {"command": "true"})
        assert docker_router.get_container_logs("hygiene-logs")["logs"] == ["line"]

        assert len(counted_service) == 1

    def test_the_service_is_the_shared_one(self, counted_service):
        """The router's service is the module singleton, not a private copy."""
        assert docker_router._service() is real_docker._get_service()
        assert docker_router._service() is docker_router._service()
        assert len(counted_service) == 1


class TestThreadPoolCeiling:
    """The worker-thread ceiling is set from config and reported."""

    async def test_the_configured_size_is_applied(self):
        with patch.object(type(config), "thread_pool_size", property(lambda _: 7)):
            applied = await configure_thread_pool()

        assert applied == 7
        assert anyio.to_thread.current_default_thread_limiter().total_tokens == 7

    async def test_startup_pins_the_limiter_to_the_configured_size(self):
        """Whatever anyio's own default is, ours is the one that ends up set."""
        applied = await configure_thread_pool()

        assert applied == config.thread_pool_size
        limiter = anyio.to_thread.current_default_thread_limiter()
        assert limiter.total_tokens == config.thread_pool_size

    def test_the_bundled_default_is_forty(self):
        """The number the code has always run at, now written down."""
        with patch.object(config, "_data", {}):
            assert config.thread_pool_size == 40

    def test_a_nonsense_size_falls_back_to_something_usable(self):
        """Zero threads would wedge every sync endpoint; one is the floor."""
        with patch.object(config, "_data", {"thread_pool_size": 0}):
            assert config.thread_pool_size == 1
