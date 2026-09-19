"""Simulated caches — the real aggregator over a simulated fleet.

There is no second implementation of the cache logic here, for the same reason
``mock.preflight`` has no second copy of the checks and ``mock.node_service``
has no second copy of the container service: a parallel implementation cannot
catch a bug in the code it stands in for.

Everything below is :mod:`spark_pulse.tools.cache` itself. It asks
``tools.node_registry`` for the nodes and ``tools.node_service`` for each one's
service, and in simulation both of those are already the mock ones — so the
aggregation, the per-node blocks, the unreachable case and the hub-cache rule
are all the production code path. What is invented is only what a node answers,
and that lives on :class:`~spark_pulse.mock.docker.MockDockerService`, one
table per simulated machine, so two nodes have two sets of caches and cleaning
one never empties the other.
"""

from __future__ import annotations

from spark_pulse.tools.cache import (  # noqa: F401 — re-exported, not re-implemented
    HUB_CACHE_NAME as HUB_CACHE_NAME,
    MAX_PARALLEL_NODES as MAX_PARALLEL_NODES,
    clean_all as clean_all,
    clean_cache as clean_cache,
    for_node as for_node,
    get_cache_dirs as get_cache_dirs,
    get_cache_status as get_cache_status,
)

__all__ = [
    "HUB_CACHE_NAME",
    "MAX_PARALLEL_NODES",
    "clean_all",
    "clean_cache",
    "for_node",
    "get_cache_dirs",
    "get_cache_status",
]
