"""Mock parallelism functions for simulation mode.

Mirrors the real parallelism.py API exactly for testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class NodeCapacity:
    """Capacity of a single node (mock)."""

    gpu_count: int

    @property
    def max_tp(self) -> int:
        """Max tensor parallelism this node can support."""
        return self.gpu_count


@dataclass(frozen=True, slots=True)
class ClusterCapacity:
    """Aggregate cluster capacity (mock)."""

    nodes: list[NodeCapacity]

    @property
    def total_gpus(self) -> int:
        """Total GPUs across all nodes."""
        return sum(n.gpu_count for n in self.nodes)

    @property
    def max_nodes(self) -> int:
        """Number of nodes in the cluster."""
        return len(self.nodes)


def parse_parallelism(command: str | object) -> dict[str, int]:
    """Extract parallelism parameters (mocked).

    Returns default values or parses from command string.

    Args:
        command: Command string or Path-like object.

    Returns:
        Dict with ``tp``, ``pp``, ``dp`` values.
    """
    result = {"tp": 1, "pp": 1, "dp": 1}

    if command is None:
        return result

    if not isinstance(command, str):
        return result

    # Split command into tokens for space-separated flag parsing
    tokens = command.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        value = None

        # Check for = format
        if "=" in token:
            key, _, val = token.partition("=")
            value = val
        elif i + 1 < len(tokens) and tokens[i + 1].lstrip("-").isdigit():
            # Space-separated: -tp 8
            value = tokens[i + 1]
            i += 1  # Skip next token

        if value is None:
            i += 1
            continue

        # Parse key
        key = token.split("=")[0].lstrip("-")
        try:
            int_val = int(value)
        except ValueError:
            i += 1
            continue

        if key in ("tp", "tensor-parallel-size"):
            result["tp"] = int_val
        elif key in ("pp", "pipeline-parallel-size"):
            result["pp"] = int_val
        elif key in ("dp", "data-parallel-size"):
            result["dp"] = int_val

        i += 1

    return result


def validate_cluster_capacity(
    parallelism: dict[str, int],
    cluster_capacity: ClusterCapacity,
) -> tuple[bool, str]:
    """Validate cluster capacity (mocked).

    Returns (True, "Cluster capacity OK") unless explicitly configured to fail.

    Args:
        parallelism: Dict with ``tp``, ``pp``, ``dp`` values.
        cluster_capacity: ClusterCapacity with node list.

    Returns:
        Tuple of (is_valid, message).
    """
    tp = parallelism.get("tp", 1)
    pp = parallelism.get("pp", 1)
    dp = parallelism.get("dp", 1)
    total_needed = tp * pp * dp
    total_available = cluster_capacity.total_gpus
    num_nodes = cluster_capacity.max_nodes
    num_groups = pp * dp

    if total_needed > total_available:
        return (
            False,
            f"Insufficient GPUs: need {total_needed}, have {total_available}",
        )

    if num_groups > num_nodes:
        return (
            False,
            f"Insufficient nodes: need {num_groups} groups (pp×dp), have {num_nodes}",
        )

    capable_nodes = sum(
        1 for n in cluster_capacity.nodes if n.gpu_count >= tp
    )
    if capable_nodes < num_groups:
        return (
            False,
            f"Only {capable_nodes} nodes support TP={tp}, need {num_groups}",
        )

    return (
        True,
        f"Cluster capacity OK: {total_available} GPUs across {num_nodes} nodes "
        f"for tp={tp} pp={pp} dp={dp} ({total_needed} total)",
    )
