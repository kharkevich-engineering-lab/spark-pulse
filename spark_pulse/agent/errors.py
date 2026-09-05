"""The three outcomes of asking a node to do something.

There are exactly three, and the whole agent exists to keep them apart:

* a **value** — the operation ran and produced an answer;
* :class:`NodeOperationError` — the operation ran and failed. The node was
  reachable, the outcome is definite, and nothing needs to be inferred;
* :class:`NodeUnreachable` — no answer arrived. The node's state is *unknown*.

The third is the one every previous transport lost. Over SSH, "the container
is not there" and "the machine did not answer" both came back as a non-zero
exit and a string, and telling them apart was a substring search that got it
wrong three times in ways that reached production
(``docs/transport-reexamined.md``). Here the distinction is structural:
outcomes travel as protocol payload, so an outcome that arrives is definite by
construction, and one that does not arrive cannot be mistaken for a failure
because there is nothing to mistake.

The rule for callers: **never convert a** :class:`NodeUnreachable` **into a
failure.** A rank on an unreachable node has not been shown to be gone, so its
GPU and ports stay held until an agent confirms otherwise (§3.3).
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "AgentError",
    "NodeUnreachable",
    "NodeOperationError",
    "EnrollmentRejected",
    "IdentityRejected",
    "UnreachableReason",
]


class AgentError(RuntimeError):
    """Base for everything this package raises."""


class UnreachableReason(str, Enum):
    """Why no answer arrived. Every value means the same thing: *unknown*.

    The distinction is for operators and logs, never for control flow. A
    caller that branches on this is reintroducing the inference the payload
    rule exists to delete.
    """

    #: The node has never connected, or is not enrolled.
    NOT_CONNECTED = "not_connected"
    #: The stream dropped while the command was in flight.
    DISCONNECTED = "disconnected"
    #: The agent held the command past the deadline without answering.
    TIMED_OUT = "timed_out"
    #: The control plane is shutting down and stopped waiting.
    SHUTTING_DOWN = "shutting_down"


class NodeUnreachable(AgentError):
    """No result arrived, so the outcome on the node is unknown.

    This is *not* a failure of the operation. The operation may have
    completed, may be running still, or may never have started.
    """

    def __init__(
        self,
        node_id: str,
        reason: UnreachableReason = UnreachableReason.NOT_CONNECTED,
        detail: str = "",
    ):
        self.node_id = node_id
        self.reason = reason
        self.detail = detail
        message = f"node {node_id} is unreachable ({reason.value})"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class NodeOperationError(AgentError):
    """The node ran the operation and it failed. Reachable, definite.

    ``error_type`` is the class name of whatever the agent caught, carried so a
    caller can react to the kind of failure without matching on the message.
    """

    def __init__(self, node_id: str, error_type: str, message: str):
        self.node_id = node_id
        self.error_type = error_type
        self.error_message = message
        super().__init__(f"{node_id}: {error_type}: {message}")


class EnrollmentRejected(AgentError):
    """A token was unknown, expired, already used, or scoped to another node."""


class IdentityRejected(AgentError):
    """A peer's certificate does not entitle it to the identity it claims."""
