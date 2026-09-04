"""Mock network tools — env var builder simulation.

Returns deterministic environment variable dicts without
accessing the system or config.
"""

from __future__ import annotations

from typing import Any


def get_env_flags(
    node_ip: str,
    eth_if: str,
    ib_if: str | None = None,
) -> dict[str, str]:
    """Simulate building NCCL/Ray/network env vars.

    Returns a deterministic dict of environment variables.
    """
    env: dict[str, str] = {
        "VLLM_HOST_IP": node_ip,
        "RAY_NODE_IP_ADDRESS": node_ip,
        "RAY_OVERRIDE_NODE_IP_ADDRESS": node_ip,
        "NCCL_SOCKET_IFNAME": eth_if if not ib_if else f"{eth_if},{ib_if}",
        "NCCL_IB_DISABLE": "false" if ib_if else "true",
        "OMPI_MCA_btl_tcp_if_include": eth_if,
        "GLOO_SOCKET_IFNAME": eth_if,
        "TP_SOCKET_IFNAME": eth_if,
        "RAY_memory_monitor_refresh_ms": "0",
        "RAY_num_prestart_python_workers": "0",
        "RAY_object_store_memory": "104857600",
        "MN_IF_NAME": eth_if,
        "UCX_NET_DEVICES": ib_if if ib_if else eth_if,
    }
    return env


def get_basic_env(
    recipe_defaults: dict[str, Any] | None = None,
    custom_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Simulate building basic env from recipe defaults."""
    env: dict[str, str] = {}
    if recipe_defaults:
        recipe_env = recipe_defaults.get("env", {})
        if recipe_env:
            env.update(recipe_env)
    if custom_env:
        env.update(custom_env)
    return env


def build_full_env(
    node_ip: str,
    eth_if: str,
    ib_if: str | None = None,
    recipe_defaults: dict[str, Any] | None = None,
    custom_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Simulate building full env."""
    env = get_basic_env(recipe_defaults, custom_env)
    env.update(get_env_flags(node_ip, eth_if, ib_if))
    return env
