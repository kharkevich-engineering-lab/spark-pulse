"""Parallelism parsing and cluster GPU capacity validation.

Extracts parallelism settings from commands/scripts and validates
that the cluster has sufficient GPU capacity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class NodeCapacity:
    """Capacity of a single node."""

    gpu_count: int

    @property
    def max_tp(self) -> int:
        """Max tensor parallelism this node can support."""
        return self.gpu_count


@dataclass(frozen=True, slots=True)
class ClusterCapacity:
    """Aggregate cluster capacity."""

    nodes: list[NodeCapacity]

    @property
    def total_gpus(self) -> int:
        """Total GPUs across all nodes."""
        return sum(n.gpu_count for n in self.nodes)

    @property
    def max_nodes(self) -> int:
        """Number of nodes in the cluster."""
        return len(self.nodes)


def parse_parallelism(command: str | Path) -> dict[str, int]:
    """Extract parallelism parameters from command or script.

    Looks for:
    - ``-tp`` / ``--tensor-parallel-size``
    - ``-pp`` / ``--pipeline-parallel-size``
    - ``-dp`` / ``--data-parallel-size``

    Args:
        command: Command string or Path to script file.

    Returns:
        Dict with ``tp``, ``pp``, ``dp`` values (default 1 each).
    """
    result = {"tp": 1, "pp": 1, "dp": 1}

    # If it's a Path, read the file content
    if isinstance(command, Path):
        if command.exists():
            command = command.read_text()
        else:
            return result

    # Handle None or empty
    if not command:
        return result

    # Pattern for --long-flag=value or --long-flag value (space-separated)
    long_pattern = re.compile(
        r"-{1,2}(?:tensor-parallel-size|tp)[=\s]+(\d+)"
    )
    pp_pattern = re.compile(
        r"-{1,2}(?:pipeline-parallel-size|pp)[=\s]+(\d+)"
    )
    dp_pattern = re.compile(
        r"-{1,2}(?:data-parallel-size|dp)[=\s]+(\d+)"
    )

    # Extract values
    tp_match = long_pattern.search(command)
    pp_match = pp_pattern.search(command)
    dp_match = dp_pattern.search(command)

    if tp_match:
        result["tp"] = int(tp_match.group(1))
    if pp_match:
        result["pp"] = int(pp_match.group(1))
    if dp_match:
        result["dp"] = int(dp_match.group(1))

    return result


def validate_cluster_capacity(
    parallelism: dict[str, int],
    cluster_capacity: ClusterCapacity,
) -> tuple[bool, str]:
    """Validate that cluster has sufficient GPU capacity.

    Evaluates:
    - available nodes
    - GPUs per node
    - total GPUs

    Examples:
    - tp=8, pp=1, dp=1 → needs 8 GPUs (can be 1 node×8 or 8 nodes×1)
    - tp=2, pp=4 → needs 8 GPUs arranged as 4 nodes×2 or 2 nodes×4
    - tp=1, pp=1, dp=4 → needs 4 nodes (each with ≥1 GPU)

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

    # Basic check: enough total GPUs
    if total_needed > total_available:
        return (
            False,
            f"Insufficient GPUs: need {total_needed}, have {total_available}",
        )

    # Check: can we arrange the parallelism across available nodes?
    # Each "group" needs tp GPUs on a single node (for tensor parallelism)
    # Number of groups = pp * dp
    num_groups = pp * dp

    if num_groups > num_nodes:
        return (
            False,
            f"Insufficient nodes: need {num_groups} groups (pp×dp), have {num_nodes}",
        )

    # Check: each node used for tensor parallelism needs ≥ tp GPUs
    for node in cluster_capacity.nodes:
        if node.gpu_count < tp and tp > 1:
            # This node can't support this TP size, but others might
            continue

    # Count how many nodes have enough GPUs for the TP size
    capable_nodes = sum(1 for n in cluster_capacity.nodes if n.gpu_count >= tp)
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
