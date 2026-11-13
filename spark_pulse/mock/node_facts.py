"""Simulated node facts: the node's profile, when no agent is connected (#174).

A test that attaches a real agent connection to the hub gets that agent's
facts, exactly as in production. Otherwise the answer is the simulated
machine's: a node whose profile reports facts (a workstation) says what its
agent would, and a Spark says nothing, as simulation always has.
"""

from __future__ import annotations

from typing import Any

from spark_pulse.tools import node_facts as _real

__all__ = ["facts_for"]


def facts_for(node_id: str) -> Any | None:
    """The connected agent's facts, else the simulated node's profile's."""
    connected = _real.facts_for(node_id)
    if connected is not None or not node_id:
        return connected
    from spark_pulse.mock import node_registry, profiles

    node = node_registry.get_node(node_id)
    if node is None:
        return None
    profile = profiles.profile_for(node.address)
    return profile.facts() if profile.reports_facts else None
