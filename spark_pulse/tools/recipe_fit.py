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

* ``tight`` — it runs, but its weights and the engine's overhead come within
  10% of a node's memory (#173).

Memory is the last input (#173): a recipe that runs by platform is held
against each node's memory — VRAM on a discrete card, the unified pool less
the host reserve on a Spark (#171) — and needing more than the node has is a
``wont_run`` that says by how much. Both halves are gathered in the
background and cached, so a listing never waits on a node or on the hub; a
figure not gathered yet leaves the verdict as the platforms made it.

Pure apart from :func:`fleet`, which reads the registry and the agents' last
heartbeat; :func:`fit` takes both as arguments so it is tested with neither.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

RUNS = "runs"
UNKNOWN = "unknown"
WONT_RUN = "wont_run"
TIGHT = "tight"

#: A need within this share of a node's memory is ``tight``.
TIGHT_MARGIN = 0.9

#: What an engine holds beyond the weights before it serves a token — the
#: CUDA context, the allocator, activations. The same figure ``vram`` uses.
RUNTIME_OVERHEAD_BYTES = 2 * 1024**3

#: How long a node's memory or a model's weight size is trusted before it is
#: gathered again. Neither changes between two page loads.
CACHE_SECONDS = 300.0


@dataclass(frozen=True)
class FleetNode:
    """One node a run could use: its id, name and platform (empty: unknown)."""

    id: str
    name: str
    platform: str = ""
    #: What a run can have of the node's memory (#171): a discrete card's
    #: VRAM, or a unified pool less the host reserve. None is unknown.
    memory_bytes: int | None = None


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


def _gb(value: float) -> str:
    return f"{value / 1e9:.0f} GB" if value >= 10e9 else f"{value / 1e9:.1f} GB"


def memory_fit(
    need_bytes: int | None, fleet_nodes: list[FleetNode], needed: int = 1
) -> Fit | None:
    """``wont_run`` or ``tight`` by memory, or None when it fits or is unknown.

    ``need_bytes`` is the recipe's weights plus the engine's overhead, split
    evenly over the ``needed`` nodes a cluster recipe spans. The nodes it
    would take are the ones with the most memory, and the smallest of those
    is the one that decides. A node whose memory is not known yet, or a need
    nobody could size, is not a verdict. Pure.
    """
    if not need_bytes:
        return None
    sized = sorted(
        (n for n in fleet_nodes if n.memory_bytes),
        key=lambda n: n.memory_bytes or 0,
        reverse=True,
    )
    if len(sized) < needed:
        return None
    weakest = sized[needed - 1]
    have = int(weakest.memory_bytes or 0)
    per_node = math.ceil(need_bytes / needed)
    where = f"{_gb(have)} on {weakest.name}"
    span = " per node" if needed > 1 else ""
    if per_node > have:
        return Fit(WONT_RUN, f"needs ~{_gb(per_node)}{span}, {where}")
    if per_node > have * TIGHT_MARGIN:
        return Fit(TIGHT, f"needs ~{_gb(per_node)}{span} of {where}")
    return None


def fit(
    recipe: dict[str, Any],
    fleet_nodes: list[FleetNode],
    variants: list[Variant],
    need_bytes: int | None = None,
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
        memory = memory_fit(need_bytes, fleet_nodes, needed)
        if memory is not None:
            return Fit(memory.verdict, memory.reason, engines=runnable)
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


def fleet(with_memory: bool = False) -> list[FleetNode]:
    """The nodes a run could use, with each one's reported platform.

    Every registered node not marked ``dead`` — the rule the Settings cluster
    block uses, so the Recipes page and the cluster switch agree on how many
    nodes there are. The platform comes from the agent's last heartbeat; a
    node with no connection, or an agent older than the field, is unknown.
    """
    from spark_pulse import tools

    try:
        nodes = [n for n in tools.node_registry.list_nodes() if n.state != "dead"]
    except Exception:  # noqa: BLE001 — a listing must not fail on the registry
        logger.debug("node registry unreadable; fitting against no nodes")
        return []
    out: list[FleetNode] = []
    for node in nodes:
        facts = tools.node_facts.facts_for(node.id)
        platform = str(getattr(facts, "platform", "") or "") if facts else ""
        out.append(
            FleetNode(
                node.id,
                node.name or node.id,
                platform,
                (
                    node_capacity(node.id, node.address, node.is_control_plane)
                    if with_memory
                    else None
                ),
            )
        )
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
    # Memory only here: the listing is what judges memory, and every other
    # caller (the plan's platform check, the pre-flight's image sizing) asks
    # about the nodes a run targets — a solo run must not reach a peer.
    nodes = fleet(with_memory=True)
    specs = variants()
    return [
        {**r, "fit": fit(r, nodes, specs, recipe_need(r)).to_dict()} for r in recipes
    ]


# ── Memory, gathered in the background ──────────────────────────────────────

_cache: dict[tuple[str, ...], tuple[float, int | None]] = {}
_pending: set[tuple[str, ...]] = set()
_cache_lock = threading.Lock()


def _cached(key: tuple[str, ...], gather: Callable[[], int | None]) -> int | None:
    """The cached figure for ``key``, refreshed in the background when stale.

    A listing must not wait on a node or the hub: the first one reads
    nothing (and the verdict stays what the platforms made it), the next
    reads what the background gathered.
    """
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        fresh = hit is not None and now - hit[0] < CACHE_SECONDS
        if fresh or key in _pending:
            return hit[1] if hit else None
        _pending.add(key)

    def work() -> None:
        try:
            value = gather()
        except Exception as exc:  # noqa: BLE001 — unknown, never an error
            logger.debug("could not gather %s: %s", key, exc)
            value = None
        with _cache_lock:
            _cache[key] = (time.monotonic(), value)
            _pending.discard(key)

    threading.Thread(target=work, name=f"recipe-fit-{key[0]}", daemon=True).start()
    return hit[1] if hit else None


def node_capacity(node_id: str, address: str, is_control: bool) -> int | None:
    """What a run can have of this node's memory, from its own stats (#171)."""
    from spark_pulse import tools

    def gather() -> int | None:
        node = tools.node_service.Node(
            id=node_id, address="" if is_control else address, is_self=is_control
        )
        memory = tools.node_stats.node_memory(tools.node_service.NodeServices()(node))
        if not memory.total_bytes:
            return None
        if memory.model == tools.node_stats.MEMORY_DISCRETE:
            return memory.total_bytes
        from spark_pulse.tools.memory_budget import OS_RESERVE_BYTES

        return max(0, memory.total_bytes - OS_RESERVE_BYTES)

    return _cached(("node", node_id), gather)


#: Weight files, in the order a repository is read for them: one format is
#: counted, never two, because a repository may carry the same weights twice.
_WEIGHT_PATTERNS = (["*.safetensors"], ["*.bin"], ["*.gguf"])


def recipe_need(recipe: dict[str, Any]) -> int | None:
    """The recipe's weights plus the engine's overhead; None until known.

    A GGUF recipe counts only the file its ``--hf-file`` names (the engine's
    own :meth:`model_patterns`, as a download does since #208); any other
    counts one format of weight files in the repository. The hub's file list
    is the source, so a model that is not downloaded yet is sized too.
    """
    model = str(recipe.get("model") or "")
    if not model:
        return None
    patterns = _engine_patterns(recipe)

    def gather() -> int | None:
        from spark_pulse import tools

        source = tools.models.get_source(None)
        for candidate in [patterns] if patterns else _WEIGHT_PATTERNS:
            size = tools.models.estimate_size(model, source, None, candidate)
            if size:
                return int(size)
        return None

    weights = _cached(("weights", model, *patterns), gather)
    return weights + RUNTIME_OVERHEAD_BYTES if weights else None


def _engine_patterns(recipe: dict[str, Any]) -> list[str]:
    from spark_pulse.engines import get_registry

    try:
        engine = get_registry().engine(str(recipe.get("engine") or ""), "default")
        return list(engine.model_patterns(recipe))
    except Exception:  # noqa: BLE001 — no patterns is "the whole repository"
        return []
