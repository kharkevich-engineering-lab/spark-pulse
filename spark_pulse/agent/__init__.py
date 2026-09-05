"""The spark-pulse node agent, and the control plane's side of it.

One implementation of every container operation, on every node, reached
through one protocol. That sentence is the entire point of this package.

``docs/transport-reexamined.md`` measured what the alternative costs: thirty
semantic divergences across fifteen methods between the two ``NodeService``
implementations, three of them live bugs, because ``service_for()`` chose a
*different implementation* per node. Anything that reintroduces a per-node
implementation choice reintroduces that.

The pieces, in the order they matter:

``agent.proto``
    The protocol. One long-lived bidirectional stream, dialled by the agent.
    Command outcomes are payload, never a gRPC status, so
    unreachable-versus-failed is structural.
``identity`` / ``enrollment`` / ``store``
    The CA, SPIFFE names, the trust-bundle pin, single-use tokens, and what a
    node keeps on disk.
``executor``
    Runs one command against this machine's ``DockerService``. The one
    implementation.
``node_agent`` / ``__main__``
    The agent process. ``python -m spark_pulse.agent``.
``server`` / ``servicer`` / ``hub`` / ``operations``
    The control plane: listeners, authentication, who is connected, and the
    internal API the rest of the control plane calls.
``local``
    The control node runs an agent too, enrolled and reached exactly like any
    other node.
``bundle`` / ``bootstrap_transport`` / ``bootstrap_probe`` / ``bootstrap``
    Getting an agent onto a node over SSH (§3.1): what is copied, the channel
    it is copied over, what the node can do, and the install itself.
``doctor``
    Why a node is not working, and what can safely be repaired from here. It
    shares its probes with ``bootstrap`` rather than growing a second set.

Names are exported lazily so that importing a submodule does not drag the
whole package — and, more practically, so that ``facts`` can import
``agent_pb2`` from this package without a cycle.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AgentHub",
    "AgentIdentity",
    "CertificateAuthority",
    "ControlPlaneServer",
    "EnrollmentLedger",
    "LocalExecutor",
    "NodeAgent",
    "NodeOperations",
    "NodeOperationError",
    "NodeUnreachable",
    "diagnose",
    "enroll",
    "install_agent",
    "remove_node_and_identity",
    "start_local_agent",
    "treat",
    "uninstall_agent_keep_identity",
]

_EXPORTS = {
    "AgentHub": ("spark_pulse.agent.hub", "AgentHub"),
    "AgentIdentity": ("spark_pulse.agent.store", "AgentIdentity"),
    "CertificateAuthority": ("spark_pulse.agent.identity", "CertificateAuthority"),
    "ControlPlaneServer": ("spark_pulse.agent.server", "ControlPlaneServer"),
    "EnrollmentLedger": ("spark_pulse.agent.enrollment", "EnrollmentLedger"),
    "LocalExecutor": ("spark_pulse.agent.executor", "LocalExecutor"),
    "NodeAgent": ("spark_pulse.agent.node_agent", "NodeAgent"),
    "NodeOperations": ("spark_pulse.agent.operations", "NodeOperations"),
    "NodeOperationError": ("spark_pulse.agent.errors", "NodeOperationError"),
    "NodeUnreachable": ("spark_pulse.agent.errors", "NodeUnreachable"),
    "diagnose": ("spark_pulse.agent.doctor", "diagnose"),
    "enroll": ("spark_pulse.agent.node_agent", "enroll"),
    "install_agent": ("spark_pulse.agent.bootstrap", "install_agent"),
    "remove_node_and_identity": (
        "spark_pulse.agent.bootstrap",
        "remove_node_and_identity",
    ),
    "start_local_agent": ("spark_pulse.agent.local", "start_local_agent"),
    "treat": ("spark_pulse.agent.doctor", "treat"),
    "uninstall_agent_keep_identity": (
        "spark_pulse.agent.bootstrap",
        "uninstall_agent_keep_identity",
    ),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    import importlib

    return getattr(importlib.import_module(module_name), attribute)


def __dir__() -> list[str]:
    return sorted(__all__)
