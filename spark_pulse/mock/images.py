"""Mock engine image catalogue.

Most of :mod:`spark_pulse.tools.images` needs no simulating: it reaches the
Docker daemon through ``spark_pulse.tools``, so the mock container service
already stands in for it, and the pull jobs run the real aggregation code over
the mock's simulated layer stream.

Two things do need canned data. The bundled engine specs carry no digests, so
nothing would ever show **digest drift** — the failure mode this page exists to
surface. And a host that has every image it needs makes the "not pulled, N GB
will download first" path invisible. Both are supplied here as an overlay on
the real catalogue.
"""

from __future__ import annotations

from typing import Any

from spark_pulse.tools.images import (  # noqa: F401 — shared machinery
    EVENT_CANCELLED,
    EVENT_COMPLETED,
    EVENT_DELETED,
    EVENT_FAILED,
    EVENT_PROGRESS,
    EVENT_QUEUED,
    EVENT_STARTED,
    EVENT_SYNCED,
    RESOURCE_TYPE,
    TERMINAL_STATES,
    PullCancelled as PullCancelled,
    cancel_pull as cancel_pull,
    clear_finished_pulls as clear_finished_pulls,
    delete_image as delete_image,
    get_pull as get_pull,
    images_in_use as images_in_use,
    list_images as _real_list_images,
    list_pulls as list_pulls,
    local_digest as local_digest,
    publish_event as publish_event,
    register_event_loop as register_event_loop,
    start_pull as start_pull,
)

# What the simulated engine index advertises. vLLM's differs from the digest
# the mock host has, so the catalogue shows one image needing an update.
_SIM_INDEX_DIGESTS: dict[str, str] = {
    "vllm/default": "sha256:" + "c3" * 32,
    "sglang/default": "sha256:" + "b2" * 32,
}

# An engine variant the simulated host has never pulled.
_SIM_MISSING_IMAGE: dict[str, Any] = {
    "ref": "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm:0.2.0",
    "repository": "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm",
    "tag": "0.2.0",
    "tagged_ref": "ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm:0.2.0",
    "engine": "vllm",
    "variant": "next",
    "engine_key": "vllm/next",
    "version": "0.2.0",
    "legacy_tags": [],
    "source": "engine-index",
    "description": "vLLM preview build — not pulled on this host",
    "present": False,
    "image_id": "",
    "size_bytes": 0,
    "created": None,
    "local_digest": "",
    "index_digest": "sha256:" + "d4" * 32,
    "digest_drift": False,
    "update_available": True,
}


def list_images() -> list[dict[str, Any]]:
    """The real catalogue, plus simulated index digests and a missing image."""
    entries = _real_list_images()
    for entry in entries:
        advertised = _SIM_INDEX_DIGESTS.get(str(entry.get("engine_key")))
        if not advertised:
            continue
        entry["index_digest"] = advertised
        entry["digest_drift"] = bool(
            entry.get("local_digest") and entry["local_digest"] != advertised
        )
        entry["update_available"] = bool(
            entry["digest_drift"] or not entry.get("present")
        )
    if not any(e["ref"] == _SIM_MISSING_IMAGE["ref"] for e in entries):
        entries.append(dict(_SIM_MISSING_IMAGE))
    entries.sort(key=lambda e: (e["source"] == "local", e["ref"]))
    return entries


def get_image(ref: str) -> dict[str, Any] | None:
    """Return a single catalogue entry by ref, or None."""
    for entry in list_images():
        if entry["ref"] == ref or entry["tagged_ref"] == ref:
            return entry
    return None


def presence(
    ref: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Every other node is pretended to have the image, with a matching ID."""
    entry = get_image(ref)
    local_id = str((entry or {}).get("image_id") or "")
    return {
        "ref": ref,
        "local": bool(entry and entry.get("present")),
        "image_id": local_id,
        "nodes": [
            {
                "node": node,
                "present": i % 2 == 0,
                "image_id": local_id if i % 2 == 0 else "",
                "matches": i % 2 == 0,
                "error": None,
            }
            for i, node in enumerate(nodes or [])
        ],
    }


def sync_to_nodes(
    ref: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Simulate ``docker save | ssh docker load``, skipping matching nodes."""
    entry = get_image(ref)
    if entry is None or not entry.get("present"):
        raise ValueError(f"Image not present locally: {ref}")
    if not nodes:
        raise ValueError("No nodes specified")
    results = [
        {
            "node": node,
            "ok": True,
            # Odd nodes are pretended to already carry the same image ID.
            "skipped": i % 2 == 1,
            "error": None,
            "duration_s": 0.4 if i % 2 else 41.2 + i,
        }
        for i, node in enumerate(nodes)
    ]
    payload = {
        "ref": ref,
        "image_id": entry.get("image_id", ""),
        "results": results,
        "ok": True,
    }
    publish_event(EVENT_SYNCED, ref, payload)
    return payload
