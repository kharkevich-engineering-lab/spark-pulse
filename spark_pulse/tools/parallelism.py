"""Parallelism parsing and cluster GPU capacity validation.

Extracts parallelism settings from commands/scripts and validates
that the cluster has sufficient GPU capacity.

The hardware this runs on has exactly **one** GPU per node — a GB10 superchip
per DGX Spark — so tensor parallelism spans nodes rather than fitting inside
one. That is the opposite of the x86 assumption (eight GPUs on a box, tensor
parallelism strictly within it) this module was first written against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


GPUS_PER_NODE = 1
"""GPUs on one DGX Spark. One GB10, one GPU — not a configurable guess."""


@dataclass(frozen=True, slots=True)
class NodeCapacity:
    """Capacity of a single node."""

    gpu_count: int = GPUS_PER_NODE

    @property
    def max_tp(self) -> int:
        """Max tensor parallelism this node can support *on its own*.

        Tensor parallelism is not confined to one node here, so this is a
        description of the node, not a limit on the launch.
        """
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

    @classmethod
    def for_nodes(
        cls, node_count: int, gpus_per_node: int = GPUS_PER_NODE
    ) -> ClusterCapacity:
        """Capacity of *node_count* identical Sparks."""
        return cls(nodes=[NodeCapacity(gpu_count=gpus_per_node)] * max(0, node_count))


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
    long_pattern = re.compile(r"-{1,2}(?:tensor-parallel-size|tp-size|tp)[=\s]+(\d+)")
    pp_pattern = re.compile(r"-{1,2}(?:pipeline-parallel-size|pp-size|pp)[=\s]+(\d+)")
    dp_pattern = re.compile(r"-{1,2}(?:data-parallel-size|dp-size|dp)[=\s]+(\d+)")

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

    Tensor parallelism may span nodes: on this hardware it has to, since a
    node holds one GPU. So a rank group is *not* required to fit inside a
    single node, and what matters is the total.

    Examples:
    - tp=8, pp=1, dp=1 → needs 8 GPUs (1 node×8, 2 nodes×4 or 8 nodes×1)
    - tp=2, pp=4 → needs 8 GPUs, in any arrangement across ≥4 nodes
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

    # Check: can we arrange the parallelism across available nodes? Each
    # pipeline/data-parallel group is a separate rank group, so there have to
    # be at least that many nodes.
    num_groups = pp * dp

    if num_groups > num_nodes:
        return (
            False,
            f"Insufficient nodes: need {num_groups} groups (pp×dp), have {num_nodes}",
        )

    # No per-node TP check: a tensor-parallel group is allowed to straddle
    # nodes, which is the only way TP>1 runs at all on one-GPU nodes.
    return (
        True,
        f"Cluster capacity OK: {total_available} GPUs across {num_nodes} nodes "
        f"for tp={tp} pp={pp} dp={dp} ({total_needed} total)",
    )
