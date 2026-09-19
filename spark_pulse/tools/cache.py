"""The engine caches, on every node, asked of every node the same way.

This module used to walk ``~/.cache`` in the control plane's own process and
``shutil.rmtree`` what it found. On one machine that reads as "the cluster"; on
two it is the control node and nothing else, and the Library's Caches section
did not say which — the last operator-facing single-node operation, and the
same defect ``tools/system.py`` had before ``GetNodeStats`` replaced it.

Now every registered node is asked for its own caches through its own agent
(``ScanCache``), including the machine this process runs on: ``service_for``
has no local branch, and the control node reaches its own agent over loopback
exactly as it reaches a peer. A node that cannot be asked keeps its section and
says why — *unknown is not empty*, because a section that vanishes and a node
with nothing cached look identical on a page and only one of them is fine.

**What stays here is the definitions.** Which directories are caches, what they
are called and what they hold are facts about the product, so they live in one
place and every node is asked the same four questions. What the control plane
cannot know is where a peer's ``$HOME`` is, so the paths travel in their ``~/``
form and each node expands them against its own — the same reasoning
``RunContainer.user``'s ``agent`` sentinel follows.

**The hub cache is the model cache.** Emptying it removes downloaded models, so
a sweep of every cache on a node (:func:`clean_all`) leaves it alone and only an
operator naming that one cache reaches it — the agent refuses it outright
without ``include_hub``, so the rule is enforced on the machine that owns the
bytes rather than trusted from here.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from spark_pulse import tools

logger = logging.getLogger(__name__)

#: How many nodes are asked at once. A cluster is small and the walk is short.
MAX_PARALLEL_NODES = 8

#: The cache directory a model download fills. Named because two rules turn on
#: it: :func:`clean_all` skips it, and cleaning it by name asks the agent to
#: lift its own refusal.
HUB_CACHE_NAME = "HF Model Cache"


def get_cache_dirs() -> list[dict[str, str]]:
    """The caches this product fills, as paths relative to a node's home.

    Engine runtime caches only — what a model server fills while it serves.
    There were three more once (``wheels``, ``.ccache``, ``uv``), all of them
    build caches belonging to upstream's wheel-building workflow, which nothing
    here has ever run: engines arrive as images and recipes come from the
    bundled set, OCI collections and the operator's own files.

    The paths are deliberately *not* expanded here. ``os.path.expanduser`` on
    the control plane answers for the control plane, and sending that to a peer
    would name a directory on the wrong machine — which is the whole defect
    this module was rewritten to fix.
    """
    return [
        {
            "name": HUB_CACHE_NAME,
            "path": "~/.cache/huggingface/hub",
            "description": "Downloaded HuggingFace models",
        },
        {
            "name": "vLLM Cache",
            "path": "~/.cache/vllm",
            "description": "vLLM internal cache",
        },
        {
            "name": "FlashInfer Cache",
            "path": "~/.cache/flashinfer",
            "description": "FlashInfer JIT cache",
        },
        {
            "name": "Triton Cache",
            "path": "~/.triton",
            "description": "Triton compiler cache",
        },
    ]


def _services(services: Any | None = None) -> Callable[[Any], Any]:
    """The resolver every node is reached through."""
    if services is not None:
        return services
    return tools.node_service.NodeServices()


def _registry_nodes() -> list[Any]:
    """The registry's records, control plane first. Empty when unreadable."""
    try:
        return list(tools.node_registry.list_nodes())
    except Exception as exc:  # noqa: BLE001 — a registry we cannot read
        logger.warning("could not read the node registry: %s", exc)
        return []


def _node_for(record: Any) -> Any:
    """The node reference for a registry record."""
    if getattr(record, "is_control_plane", False):
        return tools.node_service.control_node(address=record.address or "")
    return tools.node_service.node_for(
        record.address, ssh_user=getattr(record, "ssh_user", "") or ""
    )


def _reason(exc: BaseException) -> str:
    """Why a node could not be asked, in words an operator can act on.

    An agent too old to know ``ScanCache`` answers "command carries no op",
    which is true and unhelpful; the remedy is to update the agent, so that is
    what it says. Everything else is reported as the transport said it.
    """
    text = str(exc)[:500]
    if "carries no op" in text:
        return "this node's agent is too old to measure caches; update it"
    return text or exc.__class__.__name__


# ── One node ─────────────────────────────────────────────────────────────────


def for_node(node: Any, services: Any | None = None) -> dict[str, Any]:
    """Ask one node to measure its caches.

    The definitions and the node's answer are joined here, by position: the
    agent answers one ``CacheDir`` per requested path, in order, so the name and
    the description this module owns stay attached to the bytes the node found.
    """
    definitions = get_cache_dirs()
    resolve = _services(services)
    try:
        service = resolve(node)
        scan = service.scan_cache([entry["path"] for entry in definitions])
    except Exception as exc:  # noqa: BLE001 — every node reaches this the same way
        return {
            "reachable": False,
            "reason": _reason(exc),
            "total_bytes": 0,
            "dirs": [],
        }

    from spark_pulse.agent import codec

    measured = codec.decode_cache_scan(scan)
    dirs = []
    for definition, found in zip(definitions, measured):
        dirs.append(
            {
                "name": definition["name"],
                "description": definition["description"],
                # The node's own resolved path, not the `~/` form that was
                # sent: an operator reading a section wants to know where the
                # bytes are on *that* machine.
                "path": found["path"] or definition["path"],
                "exists": found["exists"],
                "size_bytes": found["bytes"],
                "file_count": found["files"],
                "truncated": found["truncated"],
                "error": found["error"] or None,
            }
        )
    return {
        "reachable": True,
        "reason": None,
        "total_bytes": sum(entry["size_bytes"] for entry in dirs),
        "dirs": dirs,
    }


def _labelled(record: Any, block: dict[str, Any]) -> dict[str, Any]:
    """One node's block, carrying who answered it."""
    block.update(
        {
            "node_id": getattr(record, "id", ""),
            "name": getattr(record, "name", "") or getattr(record, "address", ""),
            "address": getattr(record, "address", ""),
            "is_control_plane": bool(getattr(record, "is_control_plane", False)),
        }
    )
    return block


# ── Every node ───────────────────────────────────────────────────────────────


def get_cache_status(services: Any | None = None) -> dict[str, Any]:
    """Every registered node's caches, control plane first.

    One shape whatever the cluster size, and the same shape ``/api/memory``
    answers in — a page that reads one has read the other.
    """
    records = _registry_nodes()
    if not records:
        # No registry at all: this process still runs on a machine, and that
        # machine still has caches worth measuring.
        block = for_node(tools.node_service.control_node(), services)
        block.update(
            {
                "node_id": tools.node_service.CONTROL_NODE_ID,
                "name": "this node",
                "address": "",
                "is_control_plane": True,
            }
        )
        return {"nodes": [block]}

    def _one(record: Any) -> dict[str, Any]:
        return _labelled(record, for_node(_node_for(record), services))

    with ThreadPoolExecutor(
        max_workers=max(1, min(MAX_PARALLEL_NODES, len(records)))
    ) as pool:
        return {"nodes": list(pool.map(_one, records))}


# ── Emptying ─────────────────────────────────────────────────────────────────


def _record_for(node_id: str) -> Any | None:
    """The registry record a request named, by id or by address."""
    wanted = (node_id or "").strip()
    for record in _registry_nodes():
        if wanted in {getattr(record, "id", ""), getattr(record, "address", "")}:
            return record
    return None


def _clean(
    node_id: str,
    definitions: list[dict[str, str]],
    include_hub: bool,
    services: Any | None,
) -> dict[str, Any]:
    """Empty ``definitions`` on one node and report what each one freed."""
    if not definitions:
        return {"node": node_id, "reachable": True, "reason": None, "results": []}

    record = _record_for(node_id)
    if record is not None:
        node = _node_for(record)
    elif node_id in (tools.node_service.CONTROL_NODE_ID, "", None):
        # A control plane with no registry still has caches, and the page that
        # showed them has to be able to empty them.
        node = tools.node_service.control_node()
    else:
        return {
            "node": node_id,
            "reachable": False,
            "reason": f"no such node: {node_id}",
            "results": [],
        }

    resolve = _services(services)
    try:
        service = resolve(node)
        cleaned = service.clean_cache(
            [entry["path"] for entry in definitions], include_hub
        )
    except Exception as exc:  # noqa: BLE001 — a node that cannot be asked
        return {
            "node": node_id,
            "reachable": False,
            "reason": _reason(exc),
            "results": [],
        }

    from spark_pulse.agent import codec

    results = codec.decode_cache_clean(cleaned)
    return {
        "node": node_id,
        "reachable": True,
        "reason": None,
        "results": [
            {
                "name": definition["name"],
                "path": result["path"] or definition["path"],
                "removed": result["removed"],
                "freed_bytes": result["freed_bytes"],
                "error": result["error"] or None,
            }
            for definition, result in zip(definitions, results)
        ],
    }


def clean_cache(node_id: str, name: str, services: Any | None = None) -> dict[str, Any]:
    """Empty one named cache on one node.

    Naming the hub cache is the one way to reach it: the operator said which
    cache, so ``include_hub`` is set and the agent lifts its refusal. Nothing
    else does that — :func:`clean_all` never does.
    """
    definitions = [entry for entry in get_cache_dirs() if entry["name"] == name]
    if not definitions:
        return {
            "node": node_id,
            "reachable": True,
            "reason": None,
            "results": [
                {
                    "name": name,
                    "path": "",
                    "removed": False,
                    "freed_bytes": 0,
                    "error": f"Unknown cache: {name}",
                }
            ],
        }
    return _clean(node_id, definitions, name == HUB_CACHE_NAME, services)


def clean_all(node_id: str, services: Any | None = None) -> dict[str, Any]:
    """Empty every cache on one node except the models.

    The hub cache is left alone deliberately, and this is the reason the
    protocol carries ``include_hub`` at all: "free the space these engines are
    using" and "delete 48 GB of downloaded weights" are different requests, and
    a button that quietly does both is one an operator learns not to press.
    """
    definitions = [
        entry for entry in get_cache_dirs() if entry["name"] != HUB_CACHE_NAME
    ]
    return _clean(node_id, definitions, False, services)


__all__ = [
    "HUB_CACHE_NAME",
    "MAX_PARALLEL_NODES",
    "clean_all",
    "clean_cache",
    "for_node",
    "get_cache_dirs",
    "get_cache_status",
]
