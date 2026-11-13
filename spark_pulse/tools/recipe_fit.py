"""Whether a recipe can run on the fleet this control plane has (#172).

An x86_64 workstation was offered every recipe in the catalogue, and the first
one it deployed downloaded its model and then failed at the pull: the engine
image had no amd64 build. The answer was knowable before anything moved — the
node says which platform it is (``NodeFacts.platform``) and the engine index
says which platforms each image carries (``arch``) — so it is asked here, once
per recipe, and the Recipes page, the deploy dialog and the plan all read the
same answer.

The verdict is one of three:

* ``runs`` — some engine the recipe supports, enabled and published, has a
  variant whose image covers enough of the fleet's nodes;
* ``unknown`` — it would, but only by counting a node whose platform is not
  known (an agent too old to report it, or one not connected). Unknown is not
  a mismatch: the page shows such a recipe as runnable and says nothing;
* ``wont_run`` — with the first reason, in the order an operator can act on:
  too few nodes, no engine that supports it, no image for the fleet's
  platforms.

Memory fit (#171, #173) is the next input; it is not here yet, so a recipe
that would not fit in a node's memory still reads ``runs``.

Pure apart from :func:`fleet`, which reads the registry and the agents' last
heartbeat; :func:`fit` takes both as arguments so it is tested with neither.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

RUNS = "runs"
UNKNOWN = "unknown"
WONT_RUN = "wont_run"


@dataclass(frozen=True)
class FleetNode:
    """One node a run could use: its id, name and platform (empty: unknown)."""

    id: str
    name: str
    platform: str = ""


@dataclass(frozen=True)
class Variant:
    """One engine variant as far as fitting is concerned."""

    engine: str
    variant: str
    arch: tuple[str, ...]
    usable: bool = True


@dataclass
class Fit:
    verdict: str
    reason: str = ""
    #: engine -> the variants that run on the fleet (empty: none do).
    engines: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason, "engines": self.engines}


def nodes_needed(recipe: dict[str, Any]) -> int:
    """How many nodes the recipe's constraints ask for."""
    needed = max(1, int(recipe.get("min_nodes") or 1))
    if recipe.get("cluster_only"):
        needed = max(needed, 2)
    return needed


def _platforms_phrase(nodes: Iterable[FleetNode]) -> str:
    names = sorted({n.platform.removeprefix("linux/") for n in nodes if n.platform})
    return " or ".join(names)


def fit(
    recipe: dict[str, Any],
    fleet_nodes: list[FleetNode],
    variants: list[Variant],
) -> Fit:
    """The verdict for one recipe on ``fleet_nodes``. Pure.

    ``recipe`` is the listing payload: ``engine_support`` says which engines
    can run it at all (and why not), ``min_nodes``/``cluster_only`` how many
    nodes it needs.
    """
    needed = nodes_needed(recipe)
    if len(fleet_nodes) < needed:
        return Fit(
            WONT_RUN,
            f"needs {needed} nodes, {len(fleet_nodes)} available",
        )

    support = recipe.get("engine_support") or []
    candidates = [
        s["engine"] for s in support if s.get("supported") and s.get("enabled", True)
    ]
    if not candidates:
        refused = next((s for s in support if s.get("reason")), None)
        reason = (
            f"{refused['engine']}: {refused['reason']}"
            if refused
            else "no enabled engine runs this recipe"
        )
        return Fit(WONT_RUN, reason)

    runnable: dict[str, list[str]] = {}
    by_unknown: dict[str, list[str]] = {}
    for engine in candidates:
        for spec in variants:
            if spec.engine != engine or not spec.usable:
                continue
            arch = set(spec.arch)
            known = [n for n in fleet_nodes if n.platform and n.platform in arch]
            unknown = [n for n in fleet_nodes if not n.platform]
            if len(known) >= needed:
                runnable.setdefault(engine, []).append(spec.variant)
            elif len(known) + len(unknown) >= needed:
                by_unknown.setdefault(engine, []).append(spec.variant)

    if runnable:
        return Fit(RUNS, engines=runnable)
    if by_unknown:
        return Fit(UNKNOWN, engines=by_unknown)

    platforms = _platforms_phrase(fleet_nodes)
    published = [
        e for e in candidates if any(v.engine == e and v.usable for v in variants)
    ]
    if not published:
        return Fit(WONT_RUN, f"no published image for {' or '.join(candidates)}")
    return Fit(
        WONT_RUN,
        (
            f"no {platforms} image for {' or '.join(published)}"
            if platforms
            else f"no image for {' or '.join(published)} on this fleet"
        ),
    )


# ── The fleet and the engines, as they are now ──────────────────────────────


def fleet() -> list[FleetNode]:
    """The nodes a run could use, with each one's reported platform.

    Every registered node not marked ``dead`` — the rule the Settings cluster
    block uses, so the Recipes page and the cluster switch agree on how many
    nodes there are. The platform comes from the agent's last heartbeat; a
    node with no connection, or an agent older than the field, is unknown.
    """
    from spark_pulse import tools
    from spark_pulse.agent import runtime as agent_runtime

    try:
        nodes = [n for n in tools.node_registry.list_nodes() if n.state != "dead"]
    except Exception:  # noqa: BLE001 — a listing must not fail on the registry
        logger.debug("node registry unreadable; fitting against no nodes")
        return []
    runtime = agent_runtime.current()
    out: list[FleetNode] = []
    for node in nodes:
        agent_id = (
            runtime.control_node_id
            if runtime is not None and node.is_control_plane
            else node.id
        )
        connection = runtime.hub.get(agent_id) if runtime is not None else None
        platform = (
            str(getattr(connection.facts, "platform", "") or "") if connection else ""
        )
        out.append(FleetNode(node.id, node.name or node.id, platform))
    if not any(n for n in nodes if n.is_control_plane):
        # The machine this runs on is always one node, whether or not the
        # registry has caught up with saying so (settings counts it the same).
        out.append(FleetNode("control", "this machine", ""))
    return out


def platforms_of(addresses: list[str]) -> list[FleetNode]:
    """The nodes a plan targets, with their platforms; none named is this one.

    An empty list is a solo run on the machine this process runs on, the
    same reading the topology gives it.
    """
    nodes = fleet()
    if not addresses:
        from spark_pulse import tools

        try:
            control = next(
                (n for n in tools.node_registry.list_nodes() if n.is_control_plane),
                None,
            )
        except Exception:  # noqa: BLE001
            control = None
        wanted = control.id if control is not None else "control"
        return [n for n in nodes if n.id == wanted]
    from spark_pulse import tools

    try:
        by_address = {n.address: n.id for n in tools.node_registry.list_nodes()}
    except Exception:  # noqa: BLE001
        return []
    ids = {by_address.get(a) for a in addresses}
    return [n for n in nodes if n.id in ids]


def platform_refusal(
    engine: str, variant: str, arch: Iterable[str], targets: list[FleetNode]
) -> str:
    """Why this variant's image cannot run on ``targets``, or ``""``. Pure.

    Only a *known* platform refuses: a node that has not said what it is
    is not a mismatch, and the pull will say so if it is one.
    """
    published = sorted(set(arch))
    for node in targets:
        if node.platform and node.platform not in published:
            return (
                f"{engine}/{variant}'s image has no build for {node.platform} "
                f"({node.name}); it is published for {', '.join(published) or 'nothing'}"
            )
    return ""


def variants() -> list[Variant]:
    """Every engine variant the registry knows, with its image's platforms."""
    from spark_pulse.engines import get_registry

    try:
        registry = get_registry()
        return [
            Variant(
                spec.engine,
                spec.variant,
                tuple(spec.arch or ()),
                bool(registry.usable(spec)),
            )
            for spec in registry.list()
        ]
    except Exception:  # noqa: BLE001 — never break a listing
        logger.debug("engine registry unavailable; fitting against no engines")
        return []


def annotate(recipes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each recipe with its ``fit``, the fleet and the engines read once."""
    nodes = fleet()
    specs = variants()
    return [{**r, "fit": fit(r, nodes, specs).to_dict()} for r in recipes]
