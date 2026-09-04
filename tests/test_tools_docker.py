"""Tests for Docker tool module (simulation mode)."""

import pytest

from spark_pulse.tools import docker, is_simulation
from spark_pulse.tools.docker import (
    ContainerMetadata,
    ContainerInfo,
    DockerService,
)


class TestDockerModuleImport:
    """Test that docker module is properly accessible."""

    def test_module_has_expected_functions(self):
        """Test that the module exports expected functions."""
        assert hasattr(docker, "run_container")
        assert hasattr(docker, "stop_container")
        assert hasattr(docker, "get_container_status")
        assert hasattr(docker, "list_managed_containers")
        assert hasattr(docker, "get_container_by_deployment")

    def test_is_simulation_returns_bool(self):
        """Test that is_simulation returns a boolean."""
        result = is_simulation()
        assert isinstance(result, bool)


class TestDockerServiceSimulation:
    """Test DockerService in simulation mode."""

    def test_service_creation(self):
        """Test that DockerService can be created."""
        service = DockerService()
        assert service is not None

    def test_run_container_simulation(self):
        """Test run_container in simulation mode returns ContainerInfo."""
        if not is_simulation():
            pytest.skip("Docker daemon not available")

        metadata = ContainerMetadata(
            deployment="test-deploy",
            recipe="test-recipe",
            image="test-image:latest",
            mode="solo",
        )

        result = docker.run_container(
            image="test-image:latest",
            name="test-deploy",
            env_vars={"VLLM_MODEL": "test"},
            metadata=metadata,
        )

        assert isinstance(result, ContainerInfo)
        assert result.name == "test-deploy"
        assert result.status == "running"
        assert result.image == "test-image:latest"
        assert result.metadata.deployment == "test-deploy"

    def test_stop_container_simulation(self):
        """Test stop_container in simulation mode."""
        if not is_simulation():
            pytest.skip("Docker daemon not available")

        # Stop a non-existent container returns False
        result = docker.stop_container("nonexistent-container")
        assert result is False

    def test_list_managed_containers_simulation(self):
        """Test list_managed_containers in simulation mode."""
        if not is_simulation():
            pytest.skip("Docker daemon not available")

        # In simulation, should return empty list initially
        containers = docker.list_managed_containers()
        assert isinstance(containers, list)

    def test_get_container_status_missing(self):
        """Test get_container_status for missing container."""
        if not is_simulation():
            pytest.skip("Docker daemon not available")

        status = docker.get_container_status("nonexistent")
        assert status["status"] == "missing"
        assert status["error"] is not None


class TestDockerServiceReal:
    """Test DockerService with mock client."""

    def _skip_if_no_docker(self):
        """Skip test if docker package is not installed."""
        try:
            import docker  # noqa: F401
        except ImportError:
            pytest.skip("docker package not installed")

    def test_run_container_with_mock_client(self):
        """Test run_container with a mock Docker client."""
        self._skip_if_no_docker()
        from spark_pulse.mock.docker import MockDockerClient

        mock_client = MockDockerClient()
        service = DockerService(client=mock_client)

        metadata = ContainerMetadata(
            deployment="mock-deploy",
            recipe="mock-recipe",
            image="mock-image:latest",
            mode="solo",
            memory_limit_gb=110,
            shm_size_gb=64,
            privileged=True,
        )

        result = service.run_container(
            image="mock-image:latest",
            name="mock-deploy",
            env_vars={"TEST": "1"},
            metadata=metadata,
            privileged=True,
            memory_limit_gb=110,
            shm_size_gb=64,
        )

        assert isinstance(result, ContainerInfo)
        assert result.name == "mock-deploy"
        assert result.status == "running"

        # Verify labels were set
        assert "spark-pulse.managed" in result.metadata.to_labels()
        assert result.metadata.to_labels()["spark-pulse.managed"] == "true"

    def test_stop_container_with_mock(self):
        """Test stop_container with mock client."""
        self._skip_if_no_docker()
        from spark_pulse.mock.docker import MockDockerClient

        mock_client = MockDockerClient()
        service = DockerService(client=mock_client)

        # First run a container
        metadata = ContainerMetadata(
            deployment="stop-test",
            recipe="test",
            image="test",
        )
        service.run_container(
            image="test",
            name="stop-test",
            env_vars={},
            metadata=metadata,
        )

        # Then stop it
        result = service.stop_container("stop-test")
        assert result is True

        # Stopping again should return False
        result = service.stop_container("stop-test")
        assert result is False

    def test_list_managed_containers_with_mock(self):
        """Test list_managed_containers with mock client."""
        self._skip_if_no_docker()
        from spark_pulse.mock.docker import MockDockerClient

        mock_client = MockDockerClient()
        service = DockerService(client=mock_client)

        # Initially empty
        containers = service.list_managed_containers()
        assert len(containers) == 0

        # Run a container
        metadata = ContainerMetadata(
            deployment="list-test",
            recipe="test",
            image="test",
        )
        service.run_container(
            image="test",
            name="list-test",
            env_vars={},
            metadata=metadata,
        )

        # Should now have one container
        containers = service.list_managed_containers()
        assert len(containers) == 1
        assert containers[0].metadata.deployment == "list-test"

    def test_get_container_by_deployment_with_mock(self):
        """Test get_container_by_deployment with mock client."""
        self._skip_if_no_docker()
        from spark_pulse.mock.docker import MockDockerClient

        mock_client = MockDockerClient()
        service = DockerService(client=mock_client)

        # Run two containers
        for i in range(2):
            metadata = ContainerMetadata(
                deployment=f"deploy-{i}",
                recipe="test",
                image="test",
            )
            service.run_container(
                image="test",
                name=f"deploy-{i}",
                env_vars={},
                metadata=metadata,
            )

        # Find by deployment
        result = service.get_container_by_deployment("deploy-0")
        assert result is not None
        assert result.metadata.deployment == "deploy-0"

        # Non-existent deployment
        result = service.get_container_by_deployment("nonexistent")
        assert result is None

    def test_get_container_status_with_mock(self):
        """Test get_container_status with mock client."""
        self._skip_if_no_docker()
        from spark_pulse.mock.docker import MockDockerClient

        mock_client = MockDockerClient()
        service = DockerService(client=mock_client)

        # Running container
        metadata = ContainerMetadata(
            deployment="status-test",
            recipe="test",
            image="test",
        )
        service.run_container(
            image="test",
            name="status-test",
            env_vars={},
            metadata=metadata,
        )

        status = service.get_container_status("status-test")
        assert status["status"] == "running"
        assert status["error"] is None

        # Missing container
        status = service.get_container_status("missing")
        assert status["status"] == "missing"
