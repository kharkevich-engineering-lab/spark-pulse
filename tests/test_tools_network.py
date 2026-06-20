"""Tests for network env var builder tool."""

import pytest

from spark_pulse.tools.network import (
    get_env_flags,
    get_basic_env,
    build_full_env,
)


class TestGetEnvFlags:
    """Test environment variable generation."""

    def test_basic_env_flags(self):
        """Test basic network env vars are set."""
        env = get_env_flags("192.168.1.100", "eth0")

        assert env["VLLM_HOST_IP"] == "192.168.1.100"
        assert env["RAY_NODE_IP_ADDRESS"] == "192.168.1.100"
        assert env["RAY_OVERRIDE_NODE_IP_ADDRESS"] == "192.168.1.100"
        assert env["NCCL_SOCKET_IFNAME"] == "eth0"
        assert env["NCCL_IB_DISABLE"] == "true"

    def test_env_flags_with_infiniband(self):
        """Test env vars when InfiniBand interface is provided."""
        env = get_env_flags("192.168.1.100", "eth0", "ib0")

        assert env["NCCL_SOCKET_IFNAME"] == "eth0,ib0"
        assert env["NCCL_IB_DISABLE"] == "false"
        assert "NCCL_IB_HCA" in env
        assert env["UCX_NET_DEVICES"] == "ib0"

    def test_mpi_settings(self):
        """Test MPI environment variables."""
        env = get_env_flags("10.0.0.1", "enp3s0")
        assert env["OMPI_MCA_btl_tcp_if_include"] == "enp3s0"

    def test_gloo_settings(self):
        """Test Gloo environment variables."""
        env = get_env_flags("10.0.0.1", "enp3s0")
        assert env["GLOO_SOCKET_IFNAME"] == "enp3s0"

    def test_tp_socket_ifname(self):
        """Test TP socket ifname."""
        env = get_env_flags("10.0.0.1", "eth0")
        assert env["TP_SOCKET_IFNAME"] == "eth0"

    def test_ray_settings(self):
        """Test Ray-specific environment variables."""
        env = get_env_flags("10.0.0.1", "eth0")
        assert env["RAY_memory_monitor_refresh_ms"] == "0"
        assert env["RAY_num_prestart_python_workers"] == "0"
        assert "RAY_object_store_memory" in env

    def test_mn_if_name(self):
        """Test MN interface name."""
        env = get_env_flags("10.0.0.1", "eth0")
        assert env["MN_IF_NAME"] == "eth0"


class TestGetBasicEnv:
    """Test basic environment variable building."""

    def test_empty_inputs(self):
        """Test with no inputs returns empty dict."""
        env = get_basic_env()
        assert env == {}

    def test_recipe_defaults_only(self):
        """Test with recipe defaults only."""
        defaults = {"env": {"CUSTOM_VAR": "value1"}}
        env = get_basic_env(recipe_defaults=defaults)
        assert env["CUSTOM_VAR"] == "value1"

    def test_custom_env_only(self):
        """Test with custom env only."""
        custom = {"CUSTOM_VAR": "value2"}
        env = get_basic_env(custom_env=custom)
        assert env["CUSTOM_VAR"] == "value2"

    def test_recipe_and_custom_merged(self):
        """Test merging recipe defaults with custom env."""
        defaults = {"env": {"RECIPE_VAR": "from_recipe"}}
        custom = {"CUSTOM_VAR": "from_custom"}
        env = get_basic_env(recipe_defaults=defaults, custom_env=custom)
        assert env["RECIPE_VAR"] == "from_recipe"
        assert env["CUSTOM_VAR"] == "from_custom"

    def test_custom_overrides_recipe(self):
        """Test that custom env overrides recipe defaults."""
        defaults = {"env": {"OVERRIDE_ME": "recipe_value"}}
        custom = {"OVERRIDE_ME": "custom_value"}
        env = get_basic_env(recipe_defaults=defaults, custom_env=custom)
        assert env["OVERRIDE_ME"] == "custom_value"


class TestBuildFullEnv:
    """Test full environment building."""

    def test_full_env_contains_network_vars(self):
        """Test that full env contains network variables."""
        env = build_full_env("192.168.1.100", "eth0")
        assert "VLLM_HOST_IP" in env
        assert env["VLLM_HOST_IP"] == "192.168.1.100"

    def test_full_env_contains_basic_vars(self):
        """Test that full env contains basic variables."""
        recipe_defaults = {"env": {"BASIC_VAR": "basic_value"}}
        custom_env = {"CUSTOM_VAR": "custom_value"}
        env = build_full_env(
            "192.168.1.100",
            "eth0",
            recipe_defaults=recipe_defaults,
            custom_env=custom_env,
        )
        assert env["BASIC_VAR"] == "basic_value"
        assert env["CUSTOM_VAR"] == "custom_value"

    def test_full_env_with_infiniband(self):
        """Test full env with InfiniBand."""
        env = build_full_env("10.0.0.1", "eth0", "ib0")
        assert env["NCCL_IB_DISABLE"] == "false"
        assert env["UCX_NET_DEVICES"] == "ib0"

    def test_full_env_all_sources_merged(self):
        """Test that all env sources are merged correctly."""
        recipe_defaults = {"env": {"RECIPE": "recipe"}}
        custom_env = {"CUSTOM": "custom"}
        env = build_full_env(
            "10.0.0.1",
            "eth0",
            ib_if="ib0",
            recipe_defaults=recipe_defaults,
            custom_env=custom_env,
        )
        # From recipe
        assert env["RECIPE"] == "recipe"
        # From custom
        assert env["CUSTOM"] == "custom"
        # From network
        assert env["VLLM_HOST_IP"] == "10.0.0.1"
        assert env["NCCL_SOCKET_IFNAME"] == "eth0,ib0"
