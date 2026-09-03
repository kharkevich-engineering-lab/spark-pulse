"""Mock model catalogue — canned models and a simulated download job runner."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from spark_pulse.tools.models import (  # noqa: F401 — shared constants/helpers
    DEFAULT_SOURCES,
    EVENT_CANCELLED,
    EVENT_COMPLETED,
    EVENT_DELETED,
    EVENT_FAILED,
    EVENT_PROGRESS,
    EVENT_QUEUED,
    EVENT_STARTED,
    TERMINAL_STATES,
    publish_event,
    register_event_loop,
    repo_dir_name,
)

GB = 1024**3

_SOURCES: list[dict[str, Any]] = [
    {
        "name": "hf",
        "type": "hf_hub",
        "endpoint": "https://huggingface.co",
        "token_secret": "hf_token",
    },
    {
        "name": "mirror",
        "type": "hf_hub",
        "endpoint": "https://hf-mirror.com",
        "token_secret": "hf_mirror_token",
    },
    {
        "name": "local",
        "type": "local_path",
        "path": "/models",
    },
]


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _entry(
    model_id: str,
    *,
    revision: str,
    size_bytes: int,
    days_ago: float,
    architectures: list[str],
    model_type: str,
    torch_dtype: str = "bfloat16",
    quantization: list[str] | None = None,
    quantization_method: str | None = None,
    source: str = "hf",
    source_type: str = "hf_cache",
    path: str | None = None,
) -> dict[str, Any]:
    cfg = {
        "architectures": architectures,
        "model_type": model_type,
        "torch_dtype": torch_dtype,
        "quantization": quantization or [],
        "quantization_method": quantization_method,
    }
    snapshot = (
        path
        or f"/home/user/.cache/huggingface/hub/{repo_dir_name(model_id)}/snapshots/{revision}"
    )
    return {
        "id": model_id,
        "source": source,
        "source_type": source_type,
        "path": snapshot,
        "repo_path": f"/home/user/.cache/huggingface/hub/{repo_dir_name(model_id)}",
        "revision": revision,
        "revisions": (
            []
            if source_type == "local_path"
            else [
                {
                    "revision": revision,
                    "path": snapshot,
                    "size_bytes": size_bytes,
                    "last_modified": _iso(days_ago),
                    "refs": ["main"],
                    "config": cfg,
                }
            ]
        ),
        "size_bytes": size_bytes,
        "last_modified": _iso(days_ago),
        "config": cfg,
        "referenced_by": [],
    }


_CATALOGUE: list[dict[str, Any]] = [
    _entry(
        "openai/gpt-oss-120b",
        revision="3b2c9f1a4d5e6f708192a3b4c5d6e7f809a1b2c3",
        size_bytes=int(62.4 * GB),
        days_ago=3,
        architectures=["GptOssForCausalLM"],
        model_type="gpt_oss",
        torch_dtype="bfloat16",
    ),
    _entry(
        "Intel/Qwen3.5-397B-INT4-AutoRound",
        revision="a1f4c7e920b3d5687c1e2f3a4b5c6d7e8f901234",
        size_bytes=int(198.7 * GB),
        days_ago=11,
        architectures=["Qwen3MoeForCausalLM"],
        model_type="qwen3_moe",
        torch_dtype="bfloat16",
        quantization=["bits", "group_size", "quant_method", "sym"],
        quantization_method="auto-round",
    ),
    _entry(
        "QuantTrio/MiniMax-M2-AWQ",
        revision="7d8e9f0a1b2c3d4e5f60718293a4b5c6d7e8f901",
        size_bytes=int(114.2 * GB),
        days_ago=6,
        architectures=["MiniMaxM2ForCausalLM"],
        model_type="minimax",
        torch_dtype="float16",
        quantization=["bits", "group_size", "quant_method", "version"],
        quantization_method="awq",
    ),
    _entry(
        "meta-llama/Llama-3.3-70B-Instruct",
        revision="0c9b8a7d6e5f4132a1b0c9d8e7f6a5b4c3d2e1f0",
        size_bytes=int(141.1 * GB),
        days_ago=28,
        architectures=["LlamaForCausalLM"],
        model_type="llama",
    ),
    _entry(
        "local-team/internal-8b-sft",
        revision="local",
        size_bytes=int(16.3 * GB),
        days_ago=1,
        architectures=["LlamaForCausalLM"],
        model_type="llama",
        source="local",
        source_type="local_path",
        path="/models/local-team/internal-8b-sft",
    ),
]

_deleted: set[str] = set()
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_cancelled: set[str] = set()

_TICK_SECONDS = 0.4
_TICKS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Sources ──────────────────────────────────────────────────────────────────


def list_sources() -> list[dict[str, Any]]:
    return [dict(s) for s in _SOURCES]


def save_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    global _SOURCES
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in sources or []:
        if not isinstance(raw, dict):
            raise ValueError("Each source must be an object")
        name = str(raw.get("name", "")).strip()
        stype = str(raw.get("type", "hf_hub")).strip()
        if not name:
            raise ValueError("Source name is required")
        if name in seen:
            raise ValueError(f"Duplicate source name: {name}")
        if stype not in ("hf_hub", "local_path"):
            raise ValueError(f"Unknown source type: {stype}")
        entry: dict[str, Any] = {"name": name, "type": stype}
        if stype == "hf_hub":
            entry["endpoint"] = (
                str(raw.get("endpoint", "")).strip() or "https://huggingface.co"
            )
            entry["token_secret"] = str(raw.get("token_secret", "") or "").strip()
        else:
            path = str(raw.get("path", "")).strip()
            if not path:
                raise ValueError(f"Source '{name}' of type local_path needs a path")
            entry["path"] = path
        seen.add(name)
        cleaned.append(entry)
    _SOURCES = cleaned
    return list_sources()


def get_source(name: str | None) -> dict[str, Any]:
    sources = list_sources()
    if not name:
        return sources[0]
    for s in sources:
        if s.get("name") == name:
            return s
    raise ValueError(f"Unknown model source: {name}")


# ── Catalogue ────────────────────────────────────────────────────────────────


def hf_home():
    from pathlib import Path

    return Path("/home/user/.cache/huggingface")


def hub_dir():
    return hf_home() / "hub"


def _recipe_index() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    try:
        from spark_pulse import tools

        recipes = tools.recipes.list_recipes()
    except Exception:
        return index
    for recipe in recipes or []:
        model = str(recipe.get("model") or "").strip()
        if not model or model == "unknown":
            continue
        index.setdefault(model.lower(), []).append(
            str(recipe.get("id") or recipe.get("name") or "")
        )
    return index


def list_models() -> list[dict[str, Any]]:
    index = _recipe_index()
    out = []
    for entry in _CATALOGUE:
        if entry["id"] in _deleted:
            continue
        item = dict(entry)
        item["referenced_by"] = index.get(str(item["id"]).lower(), [])
        out.append(item)
    out.sort(key=lambda m: str(m["id"]).lower())
    return out


def get_model(model_id: str) -> dict[str, Any] | None:
    for entry in list_models():
        if entry["id"] == model_id:
            return entry
    return None


# ── Download jobs ────────────────────────────────────────────────────────────


def estimate_size(model, source, revision=None, allow_patterns=None) -> int:
    return int(12 * GB)


def check_disk_space(estimated_bytes: int, target=None) -> None:
    if estimated_bytes > 10_000 * GB:
        raise ValueError(
            f"Not enough free disk space: 512.0 GB available, "
            f"{estimated_bytes / 1e9:.1f} GB required"
        )


def list_downloads() -> list[dict[str, Any]]:
    with _jobs_lock:
        jobs = [dict(j) for j in _jobs.values()]
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return jobs


def get_download(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _set_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job.update(fields)
        return dict(job)


def _simulate(job_id: str) -> None:
    total = get_download(job_id)["bytes_total"]  # type: ignore[index]
    started = _set_job(job_id, status="running", started_at=_now())
    if started:
        publish_event(EVENT_STARTED, started["id"], started)
    for tick in range(1, _TICKS + 1):
        time.sleep(_TICK_SECONDS)
        if job_id in _cancelled:
            finished = _set_job(job_id, status="cancelled", finished_at=_now())
            if finished:
                publish_event(EVENT_CANCELLED, finished["id"], finished)
            return
        progressed = _set_job(
            job_id,
            bytes_done=int(total * tick / _TICKS),
            current_file=f"model-{tick:05d}-of-{_TICKS:05d}.safetensors",
        )
        if progressed:
            publish_event(EVENT_PROGRESS, progressed["id"], progressed)
    job = get_download(job_id)
    finished = _set_job(
        job_id,
        status="completed",
        bytes_done=total,
        current_file=None,
        path=f"{hub_dir()}/{repo_dir_name(job['model'])}/snapshots/simulated",  # type: ignore[index]
        finished_at=_now(),
    )
    if finished:
        publish_event(EVENT_COMPLETED, finished["id"], finished)


def start_download(
    model: str,
    source: str | None = None,
    revision: str | None = None,
    allow_patterns: list[str] | None = None,
) -> dict[str, Any]:
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    src = get_source(source)
    if src.get("type") != "hf_hub":
        raise ValueError(
            f"Source '{src.get('name')}' is a local path — nothing to download"
        )
    estimated = estimate_size(model, src, revision, allow_patterns)
    check_disk_space(estimated)

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "model": model,
        "source": src.get("name"),
        "endpoint": src.get("endpoint"),
        "revision": revision,
        "allow_patterns": allow_patterns or None,
        "status": "queued",
        "bytes_done": 0,
        "bytes_total": estimated,
        "current_file": None,
        "path": None,
        "error": None,
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        snapshot = dict(job)
    publish_event(EVENT_QUEUED, snapshot["id"], snapshot)
    threading.Thread(
        target=_simulate, args=(job_id,), name=f"mock-dl-{job_id}", daemon=True
    ).start()
    return snapshot


def cancel_download(job_id: str) -> dict[str, Any] | None:
    job = get_download(job_id)
    if job is None:
        return None
    if job.get("status") in TERMINAL_STATES:
        return job
    _cancelled.add(job_id)
    if job.get("status") == "queued":
        finished = _set_job(job_id, status="cancelled", finished_at=_now())
        if finished:
            publish_event(EVENT_CANCELLED, finished["id"], finished)
        return finished
    return _set_job(job_id, cancel_requested=True)


def clear_finished_downloads() -> int:
    with _jobs_lock:
        stale = [k for k, v in _jobs.items() if v.get("status") in TERMINAL_STATES]
        for k in stale:
            _jobs.pop(k, None)
    return len(stale)


# ── Distribution ─────────────────────────────────────────────────────────────


def sync_to_nodes(
    model_id: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = 3600,
) -> dict[str, Any]:
    if get_model(model_id) is None:
        raise ValueError(f"Model not in local cache: {model_id}")
    if not nodes:
        raise ValueError("No nodes specified")
    results = [
        {"node": node, "ok": True, "error": None, "duration_s": 12.5 + i}
        for i, node in enumerate(nodes)
    ]
    return {
        "model": model_id,
        "path": f"{hub_dir()}/{repo_dir_name(model_id)}",
        "results": results,
        "ok": True,
    }


def presence(
    model_id: str, nodes: list[str], ssh_user: str | None = None, timeout: int = 30
) -> dict[str, Any]:
    return {
        "model": model_id,
        "local": get_model(model_id) is not None,
        "nodes": [
            {"node": node, "present": i % 2 == 0, "error": None}
            for i, node in enumerate(nodes or [])
        ],
    }


# ── Deletion ─────────────────────────────────────────────────────────────────


def models_in_use() -> dict[str, list[str]]:
    in_use: dict[str, list[str]] = {}
    try:
        from spark_pulse import tools

        deployments = tools.deployments.list_deployments()
        recipes = {
            str(r.get("id")): str(r.get("model") or "")
            for r in (tools.recipes.list_recipes() or [])
        }
    except Exception:
        return in_use
    for dep in deployments or []:
        if dep.get("status") not in ("running", "pending"):
            continue
        params = dep.get("params") or {}
        model = str(params.get("model") or "") or recipes.get(
            str(dep.get("recipe_id")), ""
        )
        if not model or model == "unknown":
            continue
        in_use.setdefault(model.lower(), []).append(str(dep.get("id")))
    return in_use


def delete_model(model_id: str) -> dict[str, Any]:
    users = models_in_use().get(model_id.lower(), [])
    if users:
        raise ValueError(
            f"Model {model_id} is in use by running deployment(s): {', '.join(users)}"
        )
    entry = get_model(model_id)
    if entry is None:
        raise ValueError(f"Model not in local cache: {model_id}")
    _deleted.add(model_id)
    result = {
        "deleted": model_id,
        "path": entry.get("repo_path"),
        "freed_bytes": entry.get("size_bytes", 0),
    }
    publish_event(EVENT_DELETED, model_id, result)
    return result
