"""Mock parallelism functions for simulation mode.

Parallelism parsing and capacity validation are pure functions with no side
effect to simulate, so this module re-exports the real ones rather than
keeping a second copy that can drift. It exists so the real/mock module
pairing stays complete and ``tools.parallelism`` resolves in both modes.
"""

from spark_pulse.tools.parallelism import (
    GPUS_PER_NODE as GPUS_PER_NODE,
    ClusterCapacity as ClusterCapacity,
    NodeCapacity as NodeCapacity,
    parse_parallelism as parse_parallelism,
    validate_cluster_capacity as validate_cluster_capacity,
)

__all__ = [
    "GPUS_PER_NODE",
    "ClusterCapacity",
    "NodeCapacity",
    "parse_parallelism",
    "validate_cluster_capacity",
]
