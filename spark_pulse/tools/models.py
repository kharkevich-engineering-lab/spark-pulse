"""Model catalogue, download jobs and node distribution.

Independent of recipes: a model is anything present in the HuggingFace hub
cache (``$HF_HOME/hub``) or under a configured ``local_path`` source.  Recipes
are only consulted to annotate which of them reference a given model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spark_pulse.config import config
from spark_pulse.tools.events import DeploymentEvent, EventType
from spark_pulse.tools.ssh import OpenSSHClient, SSHClient, SSHError

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

TERMINAL_STATES = ("completed", "failed", "cancelled")

EVENT_QUEUED = EventType.MODEL_DOWNLOAD_QUEUED
EVENT_STARTED = EventType.MODEL_DOWNLOAD_STARTED
EVENT_PROGRESS = EventType.MODEL_DOWNLOAD_PROGRESS
EVENT_COMPLETED = EventType.MODEL_DOWNLOAD_COMPLETED
EVENT_FAILED = EventType.MODEL_DOWNLOAD_FAILED
EVENT_CANCELLED = EventType.MODEL_DOWNLOAD_CANCELLED
EVENT_DELETED = EventType.MODEL_DELETED

_PROGRESS_INTERVAL = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Event publishing ─────────────────────────────────────────────────────────
#
# Download jobs run on worker threads while the shared EventBroadcaster in
# ``sse.py`` is asyncio-based.  The ``/sse/models`` generator registers the
# running loop here when a client connects; publishes from worker threads are
# then marshalled onto that loop.  With no listener there is nothing to deliver,
# so a missing loop is not an error.

_loop: asyncio.AbstractEventLoop | None = None


def register_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Record the loop that SSE consumers run on (called from sse.py)."""
    global _loop
    _loop = loop


def publish_event(
    event_type: EventType, resource: str, metadata: dict[str, Any]
) -> None:
    """Emit a model event on the shared broadcaster from any thread."""
    from spark_pulse.sse import _get_event_broadcaster

    event = DeploymentEvent(
        event_type=event_type,
        resource=resource,
        resource_type="model",
        message=event_type.value,
        metadata=metadata,
    )
    broadcaster = _get_event_broadcaster()
    loop = _loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        running.create_task(broadcaster.emit(event))
        return
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcaster.emit(event), loop)


# ── Sources ──────────────────────────────────────────────────────────────────

DEFAULT_SOURCES: list[dict[str, Any]] = [
    {
        "name": "hf",
        "type": "hf_hub",
        "endpoint": "https://huggingface.co",
        "token_secret": "hf_token",
    }
]


def list_sources() -> list[dict[str, Any]]:
    """Return the configured model sources (defaults to the public HF hub)."""
    sources = config.model_sources
    if not sources:
        return [dict(s) for s in DEFAULT_SOURCES]
    return [dict(s) for s in sources]


def save_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and persist the model source list."""
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
    config.update(model_sources=cleaned)
    return cleaned


def get_source(name: str | None) -> dict[str, Any]:
    """Return the named source, or the first configured one when name is empty."""
    sources = list_sources()
    if not name:
        return sources[0]
    for s in sources:
        if s.get("name") == name:
            return s
    raise ValueError(f"Unknown model source: {name}")


def _source_token(source: dict[str, Any]) -> str:
    """Resolve the token for a source from the secrets store."""
    key = source.get("token_secret") or ""
    if not key:
        return ""
    if key == "hf_token":
        return config.hf_token
    return config.get_secret(key)


# ── Catalogue ────────────────────────────────────────────────────────────────


def hf_home() -> Path:
    """Return the effective HF_HOME directory."""
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")


def hub_dir() -> Path:
    """Return the HuggingFace hub cache directory."""
    return hf_home() / "hub"


def repo_dir_name(model_id: str) -> str:
    """``org/name`` -> ``models--org--name``."""
    return "models--" + model_id.replace("/", "--")


def _model_id_from_dir(dirname: str) -> str:
    return dirname[len("models--") :].replace("--", "/")


def _dir_stats(path: Path) -> tuple[int, float]:
    """Return (total size in bytes, latest mtime) for a directory tree.

    Symlinked blobs (the normal HF cache layout) are counted once via their
    resolved target so a snapshot reports its true on-disk size.
    """
    total = 0
    latest = 0.0
    seen: set[tuple[int, int]] = set()
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                fp = Path(root) / name
                try:
                    st = fp.stat()
                except OSError:
                    continue
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += st.st_size
                latest = max(latest, st.st_mtime)
    except OSError:
        pass
    return total, latest


def _config_summary(snapshot: Path) -> dict[str, Any] | None:
    """Extract the interesting bits of a model's ``config.json``."""
    cfg_file = snapshot / "config.json"
    if not cfg_file.is_file():
        return None
    try:
        with open(cfg_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    quant = data.get("quantization_config")
    return {
        "architectures": data.get("architectures") or [],
        "model_type": data.get("model_type"),
        "torch_dtype": data.get("torch_dtype"),
        "quantization": sorted(quant.keys()) if isinstance(quant, dict) else [],
        "quantization_method": (
            quant.get("quant_method") if isinstance(quant, dict) else None
        ),
    }


def _recipe_index() -> dict[str, list[str]]:
    """Map model id -> list of recipe ids referencing it."""
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


def _revisions(repo_path: Path) -> list[dict[str, Any]]:
    """List snapshot revisions of a cached repo, newest first."""
    snapshots = repo_path / "snapshots"
    if not snapshots.is_dir():
        return []
    refs: dict[str, list[str]] = {}
    refs_dir = repo_path / "refs"
    if refs_dir.is_dir():
        for ref in refs_dir.iterdir():
            if ref.is_file():
                try:
                    refs.setdefault(ref.read_text().strip(), []).append(ref.name)
                except OSError:
                    pass
    out: list[dict[str, Any]] = []
    for snap in sorted(snapshots.iterdir()):
        if not snap.is_dir():
            continue
        size, mtime = _dir_stats(snap)
        out.append(
            {
                "revision": snap.name,
                "path": str(snap),
                "size_bytes": size,
                "last_modified": (
                    datetime.fromtimestamp(mtime, timezone.utc).isoformat()
                    if mtime
                    else None
                ),
                "refs": sorted(refs.get(snap.name, [])),
                "config": _config_summary(snap),
            }
        )
    out.sort(key=lambda r: r.get("last_modified") or "", reverse=True)
    return out


def _local_source_models(source: dict[str, Any]) -> list[dict[str, Any]]:
    """List directories containing a config.json under a local_path source."""
    root = Path(os.path.expanduser(str(source.get("path", ""))))
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    candidates: list[Path] = []
    if (root / "config.json").is_file():
        candidates.append(root)
    else:
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "config.json").is_file():
                candidates.append(child)
            elif child.is_dir():
                for grandchild in sorted(child.iterdir()):
                    if grandchild.is_dir() and (grandchild / "config.json").is_file():
                        candidates.append(grandchild)
    for path in candidates:
        size, mtime = _dir_stats(path)
        model_id = str(path.relative_to(root)) if path != root else path.name
        out.append(
            {
                "id": model_id,
                "source": source.get("name"),
                "source_type": "local_path",
                "path": str(path),
                "revision": None,
                "revisions": [],
                "size_bytes": size,
                "last_modified": (
                    datetime.fromtimestamp(mtime, timezone.utc).isoformat()
                    if mtime
                    else None
                ),
                "config": _config_summary(path),
                "referenced_by": [],
            }
        )
    return out


def list_models() -> list[dict[str, Any]]:
    """Return the model catalogue: HF cache entries plus local_path sources."""
    index = _recipe_index()
    out: list[dict[str, Any]] = []
    hub = hub_dir()
    if hub.is_dir():
        for repo in sorted(hub.iterdir()):
            if not repo.is_dir() or not repo.name.startswith("models--"):
                continue
            model_id = _model_id_from_dir(repo.name)
            revisions = _revisions(repo)
            size, mtime = _dir_stats(repo)
            primary = revisions[0] if revisions else None
            out.append(
                {
                    "id": model_id,
                    "source": "hf",
                    "source_type": "hf_cache",
                    "path": primary["path"] if primary else str(repo),
                    "repo_path": str(repo),
                    "revision": primary["revision"] if primary else None,
                    "revisions": revisions,
                    "size_bytes": size,
                    "last_modified": (
                        datetime.fromtimestamp(mtime, timezone.utc).isoformat()
                        if mtime
                        else None
                    ),
                    "config": primary["config"] if primary else None,
                    "referenced_by": index.get(model_id.lower(), []),
                }
            )
    for source in list_sources():
        if source.get("type") == "local_path":
            for entry in _local_source_models(source):
                entry["referenced_by"] = index.get(str(entry["id"]).lower(), [])
                out.append(entry)
    out.sort(key=lambda m: str(m["id"]).lower())
    return out


def get_model(model_id: str) -> dict[str, Any] | None:
    """Return a single catalogue entry, or None."""
    for entry in list_models():
        if entry["id"] == model_id:
            return entry
    return None


# ── Download jobs ────────────────────────────────────────────────────────────

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_cancelled: set[str] = set()


def _publish_job(event: EventType, job: dict[str, Any]) -> None:
    publish_event(event, str(job.get("id", "")), dict(job))


def _set_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job.update(fields)
        return dict(job)


def list_downloads() -> list[dict[str, Any]]:
    """Return all known download jobs, newest first."""
    with _jobs_lock:
        jobs = [dict(j) for j in _jobs.values()]
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return jobs


def get_download(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def estimate_size(
    model: str,
    source: dict[str, Any],
    revision: str | None = None,
    allow_patterns: list[str] | None = None,
) -> int:
    """Best-effort total download size from the hub API. 0 when unknown."""
    try:
        from fnmatch import fnmatch

        from huggingface_hub import HfApi

        api = HfApi(
            endpoint=source.get("endpoint") or None, token=_source_token(source) or None
        )
        info = api.model_info(model, revision=revision, files_metadata=True)
        total = 0
        for sibling in getattr(info, "siblings", None) or []:
            name = getattr(sibling, "rfilename", "")
            if allow_patterns and not any(fnmatch(name, p) for p in allow_patterns):
                continue
            total += getattr(sibling, "size", None) or 0
        return int(total)
    except Exception:
        return 0


def check_disk_space(estimated_bytes: int, target: Path | None = None) -> None:
    """Raise ValueError when free space is below the estimated download size."""
    if estimated_bytes <= 0:
        return
    path = target or hub_dir()
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(str(probe)).free
    except OSError:
        return
    if free < estimated_bytes:
        raise ValueError(
            f"Not enough free disk space: {free / 1e9:.1f} GB available, "
            f"{estimated_bytes / 1e9:.1f} GB required at {path}"
        )


def start_download(
    model: str,
    source: str | None = None,
    revision: str | None = None,
    allow_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """Queue a snapshot download and run it on a background thread."""
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
    job: dict[str, Any] = {
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
    _publish_job(EVENT_QUEUED, snapshot)

    thread = threading.Thread(
        target=_run_download, args=(job_id, src), name=f"model-dl-{job_id}", daemon=True
    )
    thread.start()
    return snapshot


def _progress_monitor(job_id: str, repo_path: Path, stop: threading.Event) -> None:
    """Poll the repo directory size and publish progress events."""
    while not stop.wait(_PROGRESS_INTERVAL):
        size, _ = _dir_stats(repo_path)
        job = _set_job(job_id, bytes_done=size)
        if job is None or job.get("status") != "running":
            return
        _publish_job(EVENT_PROGRESS, job)


def _run_download(job_id: str, source: dict[str, Any]) -> None:
    job = get_download(job_id)
    if job is None:
        return
    if job_id in _cancelled:
        finished = _set_job(job_id, status="cancelled", finished_at=_now())
        if finished:
            _publish_job(EVENT_CANCELLED, finished)
        return

    started = _set_job(job_id, status="running", started_at=_now())
    if started:
        _publish_job(EVENT_STARTED, started)

    repo_path = hub_dir() / repo_dir_name(job["model"])
    stop = threading.Event()
    monitor = threading.Thread(
        target=_progress_monitor, args=(job_id, repo_path, stop), daemon=True
    )
    monitor.start()

    old_endpoint = os.environ.get("HF_ENDPOINT")
    try:
        from huggingface_hub import snapshot_download

        endpoint = source.get("endpoint")
        if endpoint:
            os.environ["HF_ENDPOINT"] = endpoint
        path = snapshot_download(
            repo_id=job["model"],
            revision=job.get("revision") or None,
            allow_patterns=job.get("allow_patterns") or None,
            cache_dir=str(hub_dir()),
            token=_source_token(source) or None,
        )
        stop.set()
        if job_id in _cancelled:
            finished = _set_job(job_id, status="cancelled", finished_at=_now())
            if finished:
                _publish_job(EVENT_CANCELLED, finished)
            return
        size, _ = _dir_stats(Path(path))
        finished = _set_job(
            job_id,
            status="completed",
            path=str(path),
            bytes_done=size,
            bytes_total=max(size, job.get("bytes_total") or 0),
            current_file=None,
            finished_at=_now(),
        )
        if finished:
            _publish_job(EVENT_COMPLETED, finished)
    except BaseException as exc:  # noqa: BLE001 — surface any failure on the job
        stop.set()
        if job_id in _cancelled:
            finished = _set_job(job_id, status="cancelled", finished_at=_now())
            if finished:
                _publish_job(EVENT_CANCELLED, finished)
            return
        finished = _set_job(
            job_id,
            status="failed",
            error=str(exc) or type(exc).__name__,
            finished_at=_now(),
        )
        if finished:
            _publish_job(EVENT_FAILED, finished)
    finally:
        stop.set()
        _cancelled.discard(job_id)
        if old_endpoint is None:
            os.environ.pop("HF_ENDPOINT", None)
        else:
            os.environ["HF_ENDPOINT"] = old_endpoint


def cancel_download(job_id: str) -> dict[str, Any] | None:
    """Request cancellation of a queued or running download job."""
    job = get_download(job_id)
    if job is None:
        return None
    if job.get("status") in TERMINAL_STATES:
        return job
    _cancelled.add(job_id)
    if job.get("status") == "queued":
        finished = _set_job(job_id, status="cancelled", finished_at=_now())
        if finished:
            _publish_job(EVENT_CANCELLED, finished)
        return finished
    updated = _set_job(job_id, cancel_requested=True)
    return updated


def clear_finished_downloads() -> int:
    """Drop terminal jobs from the registry. Returns how many were removed."""
    with _jobs_lock:
        stale = [k for k, v in _jobs.items() if v.get("status") in TERMINAL_STATES]
        for k in stale:
            _jobs.pop(k, None)
    return len(stale)


# ── Distribution ─────────────────────────────────────────────────────────────


def _make_ssh_client(ssh_user: str | None) -> SSHClient:
    """Build the SSH client used for distribution (overridable in tests)."""
    return OpenSSHClient(user=ssh_user or None, host_key_policy="strict")


def sync_to_nodes(
    model_id: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = 3600,
    client: SSHClient | None = None,
) -> dict[str, Any]:
    """Copy the cached model directory to each node's HF hub path, in parallel.

    Uses the shared SSH abstraction: the remote hub directory is created with a
    remote ``mkdir -p`` (the equivalent of rsync's ``--mkpath``) and the
    snapshot tree is then transferred with ``copy_dir``.
    """
    repo_path = hub_dir() / repo_dir_name(model_id)
    if not repo_path.is_dir():
        raise ValueError(f"Model not in local cache: {model_id}")
    if not nodes:
        raise ValueError("No nodes specified")

    ssh = client or _make_ssh_client(ssh_user)
    remote_dir = f"{hub_dir()}/{repo_dir_name(model_id)}"

    def _one(node: str) -> dict[str, Any]:
        started = time.monotonic()
        error: str | None = None
        try:
            result = ssh.exec(node, f"mkdir -p {remote_dir}", timeout=60)
            if not result.ok:
                error = (result.stderr or result.stdout or "mkdir failed").strip()
            else:
                ssh.copy_dir(str(repo_path), node, remote_dir, timeout=timeout)
        except (SSHError, RuntimeError, OSError) as exc:
            error = str(exc)
        return {
            "node": node,
            "ok": error is None,
            "error": error[:500] if error else None,
            "duration_s": round(time.monotonic() - started, 2),
        }

    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        results = list(pool.map(_one, nodes))

    return {
        "model": model_id,
        "path": str(repo_path),
        "results": results,
        "ok": all(r["ok"] for r in results),
    }


def presence(
    model_id: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = 30,
    client: SSHClient | None = None,
) -> dict[str, Any]:
    """Check whether the model's snapshot directory exists on each node."""
    remote_dir = f"{hub_dir()}/{repo_dir_name(model_id)}/snapshots"
    local = (hub_dir() / repo_dir_name(model_id) / "snapshots").is_dir()
    ssh = client or _make_ssh_client(ssh_user)

    def _one(node: str) -> dict[str, Any]:
        try:
            result = ssh.exec(node, f"test -d {remote_dir}", timeout=timeout)
        except (SSHError, OSError) as exc:
            return {"node": node, "present": False, "error": str(exc)[:500]}
        # A non-zero exit means "absent"; only transport failures are errors.
        error = None if result.returncode in (0, 1) else (result.stderr or "").strip()
        return {
            "node": node,
            "present": result.returncode == 0,
            "error": error[:500] if error else None,
        }

    results: list[dict[str, Any]] = []
    if nodes:
        with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
            results = list(pool.map(_one, nodes))
    return {"model": model_id, "local": local, "nodes": results}


# ── Deletion ─────────────────────────────────────────────────────────────────


def models_in_use() -> dict[str, list[str]]:
    """Map lowercased model id -> deployment ids of running/pending deployments."""
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
    """Delete a cached model directory, refusing when a deployment uses it."""
    users = models_in_use().get(model_id.lower(), [])
    if users:
        raise ValueError(
            f"Model {model_id} is in use by running deployment(s): {', '.join(users)}"
        )
    repo_path = hub_dir() / repo_dir_name(model_id)
    if not repo_path.is_dir():
        raise ValueError(f"Model not in local cache: {model_id}")
    size, _ = _dir_stats(repo_path)
    shutil.rmtree(repo_path, ignore_errors=True)
    result = {"deleted": model_id, "path": str(repo_path), "freed_bytes": size}
    publish_event(EVENT_DELETED, model_id, result)
    return result
