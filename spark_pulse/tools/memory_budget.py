"""Who holds a node's memory, and how much of it a new run can still claim.

Two runs can share a node now that a recipe's port is a preference, and the
next thing they collide on is memory. vLLM does not discover that gently: its
startup refuses with *"Free memory on device (X/Y GiB) on startup is less than
desired GPU memory utilization (F, Z GiB)"* — but only after the image has
been pulled, the container started and the weights half-read. The arithmetic
that predicts it is known before any of that, and this module is that
arithmetic.

**A claim is what a run is entitled to, not what it happens to hold.**
``--gpu-memory-utilization`` (vLLM) and ``--mem-fraction-static`` (SGLang) are
fractions of the node's *total* memory, and the engine takes all of it at
startup — so a co-tenant at 0.80 holds 0.80 of the node whatever the process
table says a second later. An engine that allocates as it goes (llama.cpp, and
any engine whose spec maps no fraction) has no entitlement to read, so its
claim is what the node measures its containers holding. A run whose claim
cannot be determined — the node did not answer, the node reports no memory for
it — is **unknown**, never zero: zero is the one answer that lets a deploy
through that should not have been.

**The total is the node's own figure.** On a DGX Spark ``nvidia-smi`` reports
no GPU memory because the pool is unified, so the total is ``MemTotal``;
where a GPU does report its own total, that is the figure the fraction is
taken of. Either way it is asked of the node, through its agent.

Everything here is arithmetic on numbers somebody else gathered, the same
stance :mod:`spark_pulse.tools.vram` takes. Nothing in this module opens a
socket or knows what a node is.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "ENGINE_DEFAULT_FRACTION",
    "FRACTION_FLAGS",
    "GIB",
    "OS_RESERVE_BYTES",
    "Holder",
    "NodeBudget",
    "budget_for_node",
    "fraction_in_command",
    "record_fraction",
]

GIB = 1024**3

#: Memory on a node that no run's fraction may count on. A GB10's memory is
#: one pool shared with the host, and the kernel, DGX OS's own services,
#: Docker, the agent and this control plane live in it outside every
#: container — a few GiB on an idle Spark. vLLM's startup check compares its
#: fraction of the *total* against what is *free*, so a budget that handed out
#: the whole total would plan runs the engine then refuses. Four GiB is that
#: few, rounded up; it is a floor for the host, not headroom for the runs.
OS_RESERVE_BYTES = 4 * GIB

#: The fraction a fraction-taking engine uses when the command passes none:
#: vLLM's own default for ``--gpu-memory-utilization``. SGLang's renderer
#: always passes ``--mem-fraction-static``, so this is vLLM's number in
#: practice — and assuming less would under-count a co-tenant.
ENGINE_DEFAULT_FRACTION = 0.9

#: The flags that carry a fraction, in every engine that maps one. Used to read
#: an older record's fraction out of its launch command.
FRACTION_FLAGS = ("--gpu-memory-utilization", "--mem-fraction-static")

#: Claims, by where the number came from.
SOURCE_FRACTION = "fraction"
SOURCE_MEASURED = "measured"
SOURCE_UNKNOWN = "unknown"


def fraction_in_command(
    command: str, flags: tuple[str, ...] = FRACTION_FLAGS
) -> float | None:
    """The fraction a rendered command passes, if it passes one.

    A regular expression rather than ``shlex``: a launch command may wrap the
    engine in ``bash -c '…'``, which ``shlex`` returns as one token with the
    flag buried inside it. The last occurrence wins, as it does for the
    engine's own argument parser.
    """
    found: float | None = None
    for flag in flags:
        for match in re.finditer(
            rf"{re.escape(flag)}(?:=|\s+)([0-9]*\.?[0-9]+)", command or ""
        ):
            try:
                value = float(match.group(1))
            except ValueError:  # pragma: no cover — the pattern only admits numbers
                continue
            if 0 < value <= 1:
                found = value
    return found


def record_fraction(record: dict[str, Any]) -> float | None:
    """The fraction a deployment record claims, or ``None`` when it takes none.

    Records written since the planner resolved it carry
    ``gpu_memory_utilization`` — ``None`` there is a positive statement that
    the engine maps no fraction. An older record does not carry the key at
    all, and its launch command is the only evidence of what was asked for; a
    command that passes no fraction leaves the claim to be measured.
    """
    if "gpu_memory_utilization" in record:
        value = record.get("gpu_memory_utilization")
        try:
            number = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return number if number is not None and 0 < number <= 1 else None
    return fraction_in_command(str(record.get("launch_command") or ""))


@dataclass
class Holder:
    """One run's claim on one node."""

    id: str
    name: str
    #: The fraction it is entitled to, when its engine takes one.
    fraction: float | None = None
    #: What that is in bytes on this node, or what the node measured. ``None``
    #: is unknown, and stays unknown.
    bytes: int | None = None
    source: str = SOURCE_UNKNOWN
    #: Why the claim is unknown, in words.
    reason: str = ""


@dataclass
class NodeBudget:
    """One node's memory, the runs already on it, and what is left.

    ``fits`` is judged on the numbers that are known; ``complete`` says
    whether every number was. The two are kept apart because they lead to
    different answers — over budget on the known claims alone is a refusal,
    while a budget with a hole in it is only a warning.
    """

    #: The address the plan names this node by; empty for this machine.
    node: str
    #: The node as the planner keys it, so a check can find its own entry.
    key: str
    label: str
    total_bytes: int | None = None
    reserve_bytes: int = OS_RESERVE_BYTES
    holders: list[Holder] = field(default_factory=list)
    held_bytes: int = 0
    left_bytes: int | None = None
    #: The largest fraction that still fits, rounded *down* to 0.01 so that
    #: taking it never lands a hair over.
    max_fraction: float | None = None
    claim_fraction: float | None = None
    claim_bytes: int | None = None
    fits: bool | None = None
    complete: bool = True
    #: Why the total is unknown, when it is.
    reason: str = ""

    @property
    def unknown(self) -> list[Holder]:
        return [h for h in self.holders if h.bytes is None]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["unknown"] = [h.id for h in self.unknown]
        return data


def budget_for_node(
    node: str,
    key: str,
    label: str,
    holders: list[tuple[str, str, float | None]],
    total_bytes: int | None,
    measured: dict[str, int | None] | None,
    claim_fraction: float | None,
    claim_bytes: int | None = None,
    reason: str = "",
    reserve_bytes: int = OS_RESERVE_BYTES,
) -> NodeBudget:
    """The budget on one node.

    ``holders`` is every other run on the node as ``(id, name, fraction)``;
    ``measured`` is what the node says each deployment's containers hold
    (``None`` for a node that could not be asked, a ``None`` value for a
    deployment whose processes report no memory). ``claim_fraction`` is the
    new run's own fraction, and ``claim_bytes`` stands in for an engine that
    takes none and has an estimate instead.
    """
    budget = NodeBudget(
        node=node,
        key=key,
        label=label,
        total_bytes=total_bytes if total_bytes and total_bytes > 0 else None,
        reserve_bytes=reserve_bytes,
        claim_fraction=claim_fraction,
        reason=reason,
    )
    total = budget.total_bytes
    for run_id, name, fraction in holders:
        holder = Holder(id=run_id, name=name or run_id, fraction=fraction)
        if fraction is not None:
            holder.source = SOURCE_FRACTION
            if total:
                holder.bytes = int(fraction * total)
            else:
                holder.reason = reason or "the node did not report its total memory"
        elif measured is None:
            holder.reason = reason or "the node could not be asked"
        elif measured.get(run_id) is not None:
            holder.source = SOURCE_MEASURED
            holder.bytes = int(measured[run_id] or 0)
        elif run_id in measured:
            holder.reason = "the node reports its process but not its memory"
        else:
            holder.reason = "the node reports no GPU process for it"
        budget.holders.append(holder)

    budget.held_bytes = sum(h.bytes or 0 for h in budget.holders)
    # Nobody else on the node is a complete answer with nothing in it.
    budget.complete = not budget.unknown and (total is not None or not holders)
    if total is None:
        budget.claim_bytes = claim_bytes
        return budget

    left = max(0, total - budget.held_bytes - reserve_bytes)
    budget.left_bytes = left
    # The epsilon keeps 0.17 from flooring to 0.16 when float division lands
    # on 0.16999…; it is far below anything a fraction can express.
    budget.max_fraction = max(0.0, math.floor(left / total * 100 + 1e-9) / 100)
    if claim_fraction is not None:
        budget.claim_bytes = int(claim_fraction * total)
    else:
        budget.claim_bytes = claim_bytes
    if budget.claim_bytes is not None:
        budget.fits = budget.claim_bytes <= left
    return budget
