"""What a node's agent last told the control plane about its machine.

One place that answers it, so simulation can answer it too (#174): every
reader used to reach into the agent hub itself, and simulation has no agent
connections, so the platform, the container runtime and the CDI devices a
node reports were never present there — every simulated node read as an agent
too old to say, and the paths keyed on them never ran.

The control node's agent is enrolled under an id of its own
(``runtime.control_node_id``) rather than the registry's, so the registry id
of the control node is translated here, once, rather than by each caller.
"""

from __future__ import annotations

from typing import Any

__all__ = ["facts_for"]


def facts_for(node_id: str) -> Any | None:
    """The ``NodeFacts`` the node's agent last sent, or ``None`` if not connected."""
    from spark_pulse.agent import runtime as agent_runtime

    current = agent_runtime.current()
    if current is None or not node_id:
        return None
    connection = current.hub.get(node_id)
    if connection is None and current.control_node_id:
        from spark_pulse import tools

        node = tools.node_registry.get_node(node_id)
        if node is not None and node.is_control_plane:
            connection = current.hub.get(current.control_node_id)
    return connection.facts if connection is not None else None
