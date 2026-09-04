"""Network environment variable builder for vLLM containers.

Builds NCCL, Ray, and network-related environment variables needed
for distributed inference on DGX Spark hardware.
"""

from __future__ import annotations

from typing import Any


def get_env_flags(
    node_ip: str,
    eth_if: str,
    ib_if: str | None = None,
) -> dict[str, str]:
    """Build NCCL/Ray/network env vars for a container.

    Args:
        node_ip: The IP address of this node (VLLM_HOST_IP).
        eth_if: The Ethernet interface name (e.g., eth0, enp3s0).
        ib_if: Optional InfiniBand interface name (e.g., ib0).

    Returns:
        Dict of environment variables for the container.
    """
    from spark_pulse.config import config

    env: dict[str, str] = {}

    # ── Core host/IP vars ────────────────────────────────────────────────
    env["VLLM_HOST_IP"] = node_ip
    env["RAY_NODE_IP_ADDRESS"] = node_ip
    env["RAY_OVERRIDE_NODE_IP_ADDRESS"] = node_ip

    # ── NCCL settings ────────────────────────────────────────────────────
    if config.nccl_socket_ifname:
        env["NCCL_SOCKET_IFNAME"] = config.nccl_socket_ifname
    elif ib_if:
        # If no explicit setting and IB is available, prefer IB
        env["NCCL_SOCKET_IFNAME"] = f"{eth_if},{ib_if}"
    else:
        env["NCCL_SOCKET_IFNAME"] = eth_if

    if ib_if:
        # Set InfiniBand HCA selectors
        if config.nccl_ib_hca:
            env["NCCL_IB_HCA"] = config.nccl_ib_hca
        else:
            # Default: select all IB HCAs with good throughput
            env["NCCL_IB_HCA"] = "GPU"
        env["NCCL_IB_DISABLE"] = "false"
        env["NCCL_IB_GID_INDEX"] = "3"
        env["NCCL_IB_TIMEOUT"] = "22"
        env["NCCL_IB_RETRY_CNT"] = "7"
    else:
        env["NCCL_IB_DISABLE"] = "true"

    # NCCL debug level
    if config.nccl_debug:
        env["NCCL_DEBUG"] = config.nccl_debug

    # ── MPI / OpenMPI settings ───────────────────────────────────────────
    env["OMPI_MCA_btl_tcp_if_include"] = eth_if

    # ── Gloo (used by Ray/Distributed) ───────────────────────────────────
    env["GLOO_SOCKET_IFNAME"] = eth_if

    # ── TP (Tensor Parallelism) socket ifname ────────────────────────────
    env["TP_SOCKET_IFNAME"] = eth_if

    # ── Ray settings ─────────────────────────────────────────────────────
    env["RAY_memory_monitor_refresh_ms"] = "0"
    env["RAY_num_prestart_python_workers"] = "0"
    env["RAY_object_store_memory"] = "104857600"  # 100MB

    # ── MN (Multi-node) interface name ───────────────────────────────────
    env["MN_IF_NAME"] = eth_if

    # ── UCX (Unified Communication X) for RDMA ───────────────────────────
    if ib_if:
        env["UCX_NET_DEVICES"] = ib_if
    else:
        env["UCX_NET_DEVICES"] = eth_if

    return env


def get_basic_env(
    recipe_defaults: dict[str, Any] | None = None,
    custom_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build basic environment variables from recipe defaults.

    Args:
        recipe_defaults: Default env vars from the recipe YAML.
        custom_env: Custom env vars to merge on top.

    Returns:
        Merged environment variable dict.
    """
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
    """Build complete environment for a container.

    Merges network env vars with recipe defaults and custom env.

    Args:
        node_ip: Node IP address.
        eth_if: Ethernet interface.
        ib_if: Optional InfiniBand interface.
        recipe_defaults: Recipe default env vars.
        custom_env: Custom env vars.

    Returns:
        Complete merged environment dict.
    """
    env = get_basic_env(recipe_defaults, custom_env)
    network_env = get_env_flags(node_ip, eth_if, ib_if)
    env.update(network_env)
    return env
