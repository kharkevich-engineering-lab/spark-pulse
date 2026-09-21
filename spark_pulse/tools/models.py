"""Model catalogue, download jobs and node distribution.

Independent of recipes: a model is anything present in the HuggingFace hub
cache (``$HF_HOME/hub``) or under a configured ``local_path`` source.  Recipes
are only consulted to annotate which of them reference a given model.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from spark_pulse.config import config
from spark_pulse.tools import hub_cache
from spark_pulse.tools.events import DeploymentEvent, EventType
from spark_pulse.tools.ssh import (
    OpenSSHClient,
    SSHClient,
    SSHError,
    known_hosts_path,
)

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

EVENT_REPLICATION_STARTED = EventType.MODEL_REPLICATION_STARTED
EVENT_REPLICATION_PROGRESS = EventType.MODEL_REPLICATION_PROGRESS
EVENT_REPLICATION_VERIFIED = EventType.MODEL_REPLICATION_VERIFIED
EVENT_REPLICATION_FAILED = EventType.MODEL_REPLICATION_FAILED

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


#: ``org/name`` -> ``models--org--name``.  Defined once, in the standalone
#: layout module, because the node-side verifier needs it without importing
#: spark_pulse.
repo_dir_name = hub_cache.repo_dir_name


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
        # The attention shape, for the KV-cache half of the VRAM estimate.
        # Carried verbatim rather than interpreted: what these fields *mean*
        # is `tools.vram`'s business, and a catalogue that decided which
        # attention layout a model used would have to be edited every time a
        # new one appeared. Absent keys stay absent; the estimator reports an
        # unknown rather than defaulting.
        "num_hidden_layers": data.get("num_hidden_layers"),
        "num_attention_heads": data.get("num_attention_heads"),
        "num_key_value_heads": data.get("num_key_value_heads"),
        "hidden_size": data.get("hidden_size"),
        "head_dim": data.get("head_dim"),
        "kv_lora_rank": data.get("kv_lora_rank"),
        "qk_rope_head_dim": data.get("qk_rope_head_dim"),
        "max_position_embeddings": data.get("max_position_embeddings"),
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


#: Called once per download job when it reaches a terminal state.
#:
#: A hook and not an event subscription, deliberately. The event stream is
#: async — ``publish_event`` hands work to an ``asyncio`` loop for SSE
#: subscribers — while a download runs on a plain daemon thread, and something
#: that must act on completion (deploy the thing that was waiting for it)
#: cannot be left to whether a loop is registered and running. This fires on
#: the download thread, synchronously, whether or not anybody is watching.
_finish_hooks: list[Callable[[dict[str, Any]], None]] = []

#: Jobs whose hooks have already run. Several code paths can publish the same
#: terminal state for one job — a queued job cancelled by the API is settled
#: by ``cancel_download`` and again by the thread that picks it up — and a
#: hook that starts a deployment must not run twice.
_notified: set[str] = set()


def add_finish_listener(hook: Callable[[dict[str, Any]], None]) -> None:
    """Register ``hook`` to be called when any download job finishes."""
    if hook not in _finish_hooks:
        _finish_hooks.append(hook)


def _notify_finished(job: dict[str, Any]) -> None:
    job_id = str(job.get("id") or "")
    if not job_id:
        return
    with _jobs_lock:
        if job_id in _notified:
            return
        _notified.add(job_id)
    for hook in list(_finish_hooks):
        try:
            hook(dict(job))
        except Exception:  # noqa: BLE001 — a bad listener must not lose the job
            logger.exception("download finish listener failed for job %s", job_id)


def _publish_job(event: EventType, job: dict[str, Any]) -> None:
    publish_event(event, str(job.get("id", "")), dict(job))
    if job.get("status") in TERMINAL_STATES:
        _notify_finished(job)


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


def _plan_files(
    model: str,
    source: dict[str, Any],
    revision: str | None = None,
    allow_patterns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The job's own file set: what the hub lists, narrowed by the patterns.

    This is the difference between a download that can be stopped and one that
    cannot. With a plan the worker fetches one file at a time and looks at the
    cancel flag between them; without one it falls back to a single
    ``snapshot_download`` that runs to completion whatever is asked of it.

    It is also what ``bytes_done`` is counted against. The whole cache entry is
    not this job's business — blobs a wider, earlier download left behind live
    in the same directory, which is how a 7.2 GB filtered download came to
    report 10.3 GB done.

    Empty when the hub will not answer; the caller then takes the fallback.
    ``fnmatch`` on the repo-relative path is the same rule
    ``huggingface_hub`` filters ``allow_patterns`` by.
    """
    try:
        from fnmatch import fnmatch

        from huggingface_hub import HfApi

        api = HfApi(
            endpoint=source.get("endpoint") or None, token=_source_token(source) or None
        )
        info = api.model_info(model, revision=revision, files_metadata=True)
        plan: list[dict[str, Any]] = []
        for sibling in getattr(info, "siblings", None) or []:
            name = getattr(sibling, "rfilename", "")
            if not name:
                continue
            if allow_patterns and not any(fnmatch(name, p) for p in allow_patterns):
                continue
            plan.append(
                {"path": name, "size": int(getattr(sibling, "size", None) or 0)}
            )
        return plan
    except Exception:
        return []


def _plan_bytes(plan: list[dict[str, Any]]) -> int:
    return sum(int(entry.get("size") or 0) for entry in plan)


def estimate_size(
    model: str,
    source: dict[str, Any],
    revision: str | None = None,
    allow_patterns: list[str] | None = None,
) -> int:
    """Best-effort total download size from the hub API. 0 when unknown."""
    return _plan_bytes(_plan_files(model, source, revision, allow_patterns))


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


#: A job in one of these has not finished and is still writing to the cache.
_ACTIVE_DOWNLOAD_STATES = ("queued", "running")


def _active_download_for(model: str) -> dict[str, Any] | None:
    """An unfinished job for this model, whatever it was asked to fetch.

    Keyed on the model alone because the model is what decides *which cache
    entry is being written*: one ``models--org--name`` directory, one set of
    ``.locks``, one blob store. Two jobs there are not two downloads whatever
    revision or mirror they name.
    """
    with _jobs_lock:
        for job in _jobs.values():
            if (
                job.get("status") in _ACTIVE_DOWNLOAD_STATES
                and job.get("model") == model
            ):
                return dict(job)
    return None


def _same_request(
    job: dict[str, Any],
    source: str | None,
    revision: str | None,
    allow_patterns: list[str] | None,
) -> bool:
    """Whether a running job is fetching exactly what is being asked for."""
    return (
        job.get("source") == source
        and (job.get("revision") or None) == (revision or None)
        and list(job.get("allow_patterns") or []) == list(allow_patterns or [])
    )


class DownloadInProgress(RuntimeError):
    """A *different* download of this model is already running.

    Handing back the running job when the request is identical is right: it is
    the same bytes into the same directory, and a deploy that offers to fetch a
    missing model has to be safe to retry. Handing it back when the caller
    asked for different files, a different revision or a different mirror is
    answering a question nobody asked — an operator who cancelled a 68 GB
    unfiltered download and started a filtered one would be handed the job they
    were trying to get away from. That is a 409, naming the job to cancel.
    """

    def __init__(self, job: dict[str, Any]):
        self.job = dict(job)
        super().__init__(
            f"{job.get('model')} is already downloading as job "
            f"{job.get('id')}; cancel it before starting a different download "
            "of the same model"
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

    # Already downloading? Two jobs for one model are not two downloads: they
    # write into the same HuggingFace cache directory, so both fetch the same
    # files, both report the size of that shared directory as their own
    # progress, and cancelling one leaves the other running. What an operator
    # sees is the same model listed twice at identical byte counts.
    #
    # The same request gets the running job back — a deploy that offers to
    # fetch a missing model has to be safe to retry. A *different* one gets a
    # refusal instead: see ``DownloadInProgress``.
    existing = _active_download_for(model)
    if existing is not None:
        if not _same_request(existing, src.get("name"), revision, allow_patterns):
            raise DownloadInProgress(existing)
        logger.info(
            "download of %s is already %s (job %s); returning it rather than "
            "starting a second",
            model,
            existing.get("status"),
            existing.get("id"),
        )
        return existing

    plan = _plan_files(model, src, revision, allow_patterns)
    estimated = _plan_bytes(plan)
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
        "files_total": len(plan),
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

    # The plan travels as a thread argument rather than on the job: it is
    # thirty paths of bookkeeping, and the job is what every SSE frame and
    # every `/api/models/downloads` response carries.
    thread = threading.Thread(
        target=_run_download,
        args=(job_id, src, plan),
        name=f"model-dl-{job_id}",
        daemon=True,
    )
    thread.start()
    return snapshot


class _DownloadCancelled(Exception):
    """The operator asked this job to stop, and the loop between files did."""


class _PlanProgress:
    """How much of *this job's* file set has landed.

    The whole-directory poll this replaces counted every byte in the cache
    entry, blobs a wider earlier download left behind included: cancel an
    unfiltered 68 GB job, start a filtered 7.2 GB one into the same directory,
    and the new job reports 10.3 GB of 7.2 GB done. Here nothing outside the
    plan is counted and no file counts for more than its own size, so
    ``bytes_done`` cannot pass ``bytes_total`` — including on a resume, where
    the bytes were already there when the job started.
    """

    def __init__(self, entries: list[dict[str, Any]]):
        self.entries = list(entries)
        self.total = _plan_bytes(self.entries)
        self._lock = threading.Lock()
        self._done = 0
        self._current = 0

    @property
    def done(self) -> int:
        with self._lock:
            return self._done

    def begin(self, expected: int) -> None:
        """A file is in flight, and it weighs this much in the plan."""
        with self._lock:
            self._current = max(0, expected)

    def finish(self, landed: int) -> None:
        with self._lock:
            self._done += max(0, landed)
            self._current = 0

    def bytes_done(self, repo_path: Path) -> int:
        """Finished files, plus what the file in flight has landed so far."""
        with self._lock:
            done, current = self._done, self._current
        if current:
            done += min(_incomplete_bytes(repo_path), current)
        return min(done, self.total) if self.total else done


def _incomplete_bytes(repo_path: Path) -> int:
    """The largest half-written blob under the entry, or 0.

    ``hf_hub_download`` writes into ``blobs/<hash>.incomplete`` and renames on
    success, so this is how far the file in flight has got — and, on a resume,
    what a cancelled job already fetched of it. The largest rather than the
    sum: a leftover from some earlier cancelled file is still sitting there,
    and counting both would report bytes twice.
    """
    try:
        return max(
            (
                path.stat().st_size
                for path in (repo_path / "blobs").glob("*.incomplete")
            ),
            default=0,
        )
    except OSError:
        return 0


def _progress_monitor(
    job_id: str,
    repo_path: Path,
    stop: threading.Event,
    plan: _PlanProgress | None = None,
) -> None:
    """Poll how far the job has got and publish progress events."""
    while not stop.wait(_PROGRESS_INTERVAL):
        if plan is not None:
            size = plan.bytes_done(repo_path)
        else:
            size, _ = _dir_stats(repo_path)
        job = _set_job(job_id, bytes_done=size)
        if job is None or job.get("status") != "running":
            return
        _publish_job(EVENT_PROGRESS, job)


def _download_error(exc: BaseException) -> str:
    """The job's ``error``, with the remedy when the cache is not ours.

    ``[Errno 13] Permission denied: …/.locks/models--…`` is what a download
    against a hub cache an engine container wrote as root looks like: Hugging
    Face cannot take its own lock. The errno alone sends the operator looking
    for a bug in the downloader. Naming the directory and the command is the
    difference between a report and an instruction.
    """
    message = str(exc) or type(exc).__name__
    errno_value = getattr(exc, "errno", None)
    if not isinstance(exc, PermissionError) and errno_value not in (
        errno.EACCES,
        errno.EPERM,
    ):
        return message
    hub = str(hub_dir())
    return (
        f"{message} — the Hugging Face cache at {hub} is not writable by the "
        f"user running spark-pulse, which is what an engine container that ran "
        f"as root leaves behind; {hub_cache.chown_remedy(hub)}"
    )


def _snapshot_root(local_file: str, rel_path: str) -> str:
    """``…/snapshots/<commit>`` from one file inside it and its repo path."""
    root = local_file
    for _ in range(rel_path.count("/") + 1):
        root = os.path.dirname(root)
    return root


def _landed(local_file: str, expected: int) -> int:
    """What this file adds to ``bytes_done``, never more than its own size."""
    try:
        actual = os.path.getsize(local_file)
    except OSError:  # pragma: no cover — the file was just written
        actual = expected
    return min(actual, expected) if expected else actual


def _download_plan(
    job_id: str,
    job: dict[str, Any],
    source: dict[str, Any],
    plan: _PlanProgress,
) -> str:
    """Fetch the job's files one at a time, stopping when asked to.

    ``snapshot_download`` is one blocking call with nowhere to put a cancel
    check: cancelling a 68 GB download did nothing at all — twenty requests
    over two minutes, and only restarting the control plane stopped it. One
    ``hf_hub_download`` per file gives the loop a place to look at the flag,
    between files, which is the granularity the bytes already have.

    Nothing is cleaned up on the way out. The interrupted file's
    ``blobs/<hash>.incomplete`` is exactly what the next job resumes from, and
    the files already fetched are real files somebody may still want.
    """
    from huggingface_hub import hf_hub_download

    token = _source_token(source) or None
    endpoint = source.get("endpoint") or None
    snapshot = ""
    for entry in plan.entries:
        if job_id in _cancelled:
            raise _DownloadCancelled(job_id)
        rel_path = str(entry.get("path") or "")
        expected = int(entry.get("size") or 0)
        plan.begin(expected)
        _set_job(job_id, current_file=rel_path)
        # ``endpoint`` is passed straight to the call rather than through the
        # ``HF_ENDPOINT`` environment variable: the variable is process-global
        # but downloads run on concurrent daemon threads, so two downloads
        # from different sources racing each other would clobber each other's
        # endpoint mid-flight.
        local_file = hf_hub_download(
            repo_id=job["model"],
            filename=rel_path,
            revision=job.get("revision") or None,
            cache_dir=str(hub_dir()),
            token=token,
            endpoint=endpoint,
        )
        plan.finish(_landed(local_file, expected))
        snapshot = snapshot or _snapshot_root(local_file, rel_path)
    if job_id in _cancelled:
        raise _DownloadCancelled(job_id)
    return snapshot or str(hub_dir() / repo_dir_name(job["model"]))


def _record_download(job: dict[str, Any], snapshot_path: str) -> None:
    """Record an ``allow_patterns`` download beside the cache entry it made.

    Only a filtered one. A download of the whole revision needs no marker:
    the manifest already says what should be there. A filtered one is a
    deliberate subset — one GGUF quantisation out of nine — and nothing on
    disk said so, so the verifier counted the eight nobody asked for as
    missing and called a finished download ``partial`` for ever after, which
    also made it unreplicable. The marker names the filter; ``hub_cache``
    reads it back and expects what was asked for.
    """
    patterns = list(job.get("allow_patterns") or [])
    if not patterns:
        return
    repo = str(hub_dir() / repo_dir_name(job["model"]))
    commit = os.path.basename(snapshot_path.rstrip("/"))
    if not hub_cache.is_commit_hash(commit):
        commit = hub_cache.resolve_commit(repo, job.get("revision") or None) or ""
    if not commit:
        return
    try:
        hub_cache.write_marker(
            repo,
            {
                "model": job.get("model"),
                "revision": commit,
                "bytes": int(job.get("bytes_done") or 0),
                "files": int(job.get("files_total") or 0),
                "evidence": hub_cache.EVIDENCE_DOWNLOAD,
                "allow_patterns": patterns,
                "source": job.get("source") or "",
            },
        )
    except OSError as exc:
        # A marker that could not be written is a verifier that will call this
        # entry partial — worth a line in the log, never worth failing a
        # download that has already landed.
        logger.warning("could not record the download marker for %s: %s", repo, exc)


def _run_download(
    job_id: str,
    source: dict[str, Any],
    plan: list[dict[str, Any]] | None = None,
) -> None:
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
    progress = _PlanProgress(plan) if plan else None
    stop = threading.Event()
    monitor = threading.Thread(
        target=_progress_monitor,
        args=(job_id, repo_path, stop, progress),
        daemon=True,
    )
    monitor.start()

    try:
        if progress is not None:
            path = _download_plan(job_id, job, source, progress)
            done = progress.done
        else:
            from huggingface_hub import snapshot_download

            # No file list from the hub, so no loop to check the cancel flag
            # in: this is one blocking call that always runs to completion.
            # The bytes are on disk regardless of what was asked, so reporting
            # it "cancelled" would call a real, usable download a failure and
            # fail any scheduled deploy waiting on a model that has, in fact,
            # arrived. Only a cancel made *before* the call started (checked
            # above, and on the exception path below) stops anything.
            path = snapshot_download(
                repo_id=job["model"],
                revision=job.get("revision") or None,
                allow_patterns=job.get("allow_patterns") or None,
                cache_dir=str(hub_dir()),
                token=_source_token(source) or None,
                endpoint=source.get("endpoint") or None,
            )
            done, _ = _dir_stats(Path(path))
        stop.set()
        finished = _set_job(
            job_id,
            status="completed",
            path=str(path),
            bytes_done=done,
            bytes_total=max(done, job.get("bytes_total") or 0),
            current_file=None,
            cancel_requested=False,
            finished_at=_now(),
        )
        if finished:
            _record_download(finished, str(path))
            _publish_job(EVENT_COMPLETED, finished)
    except BaseException as exc:  # noqa: BLE001 — surface any failure on the job
        stop.set()
        if job_id in _cancelled:
            finished = _set_job(
                job_id,
                status="cancelled",
                current_file=None,
                cancel_requested=False,
                finished_at=_now(),
            )
            if finished:
                _publish_job(EVENT_CANCELLED, finished)
            return
        finished = _set_job(
            job_id,
            status="failed",
            error=_download_error(exc),
            finished_at=_now(),
        )
        if finished:
            _publish_job(EVENT_FAILED, finished)
    finally:
        stop.set()
        _cancelled.discard(job_id)


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
            _notified.discard(k)
    return len(stale)


# ── Distribution ─────────────────────────────────────────────────────────────
#
# Replication copies a *cache entry*, not a snapshot.  ``blobs`` holds the
# bytes, ``snapshots/<commit>`` holds relative symlinks into them, ``refs``
# names the commit and ``trees/<commit>.json`` is the manifest that makes the
# result checkable.  All four go together, in one rsync run, because every
# subset is broken: snapshots without blobs dangles, blobs without snapshots is
# unusable, and a copy made by a tool that does not preserve symlinks arrives
# as an empty snapshot that HuggingFace then silently re-downloads in full.
#
# Nothing is published until it has been verified *on the node*, and nothing
# treats a path existing as proof that it is ready.


def _control_plane_identity() -> str | None:
    """Path to the control-plane key every enrolled node already trusts.

    Each install leaves this key's public half in the node's
    ``authorized_keys`` (``agent.onboarding``), so replication authenticates
    with the one identity the cluster is built around rather than whatever
    personal SSH key the control-plane user happens to have — on a fresh box it
    has none, and rsync would fail for the want of it. Returns ``None`` when the
    agent runtime is not up (tests, an import-time call), and ssh then falls
    back to its default identity as before.
    """
    try:
        from spark_pulse.agent import runtime as agent_runtime
        from spark_pulse.agent.bootstrap import control_plane_keypair
    except Exception:  # pragma: no cover — the agent stack is always importable
        return None
    current = agent_runtime.current()
    if current is None:
        return None
    # Ensures the key exists on disk (idempotent) and returns the keypair; we
    # want its file path to hand ssh with -i.
    control_plane_keypair(current.server)
    return str(current.server.directory / "bootstrap" / "id_ed25519")


def _make_ssh_client(ssh_user: str | None) -> SSHClient:
    """Build the SSH client used for distribution (overridable in tests).

    Strict host-key checking, verified against the control plane's own
    known_hosts — the keys an operator confirmed during bootstrap, written
    there by ``agent.bootstrap.install_agent``. Before that file existed this
    client had never heard of any node and every rsync failed on
    ``No ED25519 host key is known``, which is what made ``ssh-keyscan`` the
    workaround on a real cluster; trusting whatever answers is the opposite of
    what the fingerprint confirmation is for.
    """
    return OpenSSHClient(
        user=ssh_user or None,
        host_key_policy="strict",
        identity_file=_control_plane_identity(),
        known_hosts_file=str(known_hosts_path()),
    )


#: Where a transfer lands before it has earned the right to be the real thing.
#: Under the hub directory so it shares the hub's filesystem, which is what
#: makes the publishing rename atomic rather than a copy.
STAGING_DIRNAME = ".spark-pulse-staging"

#: The verifier is copied to the node and run there by the node's own python.
#: Verifying on the control node would only prove that the control node's copy
#: is fine, which was never in doubt.
REMOTE_HELPER_NAME = "hub_cache.py"
REMOTE_PYTHON = "python3"

#: How often the progress poll asks a node how many bytes have landed.
REPLICATION_POLL_INTERVAL = 5.0

#: Seconds allowed for the small remote commands (mkdir, verify, rename), as
#: opposed to the transfer itself.
CONTROL_COMMAND_TIMEOUT = 300


def _helper_source() -> Path:
    """The local path of the standalone verifier that gets shipped to nodes."""
    return Path(hub_cache.__file__)


def _staging_root() -> str:
    return f"{hub_dir()}/{STAGING_DIRNAME}"


def _remote_helper_path() -> str:
    return f"{_staging_root()}/{REMOTE_HELPER_NAME}"


def _q(value: str) -> str:
    return shlex.quote(str(value))


def local_repo_path(model_id: str) -> Path:
    """The local cache entry for ``model_id``."""
    return hub_dir() / repo_dir_name(model_id)


#: ``hf cache verify`` asks the hub for the revision's file list and compares
#: the local files against the hashes the hub publishes — SHA-256 for LFS/Xet
#: files, git SHA-1 for the rest. It is the most authoritative check available,
#: and it is only available *here*: it needs the network and, for a gated repo,
#: the token. Both are things a worker node deliberately does not have, which
#: is why the node-side check reads ``trees/<commit>.json`` — the same hashes,
#: cached locally by the download that fetched them.
HF_CLI = "hf"
HF_CLI_TIMEOUT = 900


def verify_local(
    model_id: str,
    revision: str | None = None,
    deep: bool = False,
    use_cli: bool = False,
) -> dict[str, Any]:
    """Verify the control node's own copy of a model.

    Args:
        model_id: The cached model to check.
        revision: Commit or ref; ``None`` resolves ``refs/main``.
        deep: Hash every file, not only compare sizes.
        use_cli: Additionally cross-check against the hub with
            ``hf cache verify``. Requires the network and the token, so it is
            off by default and never runs on a node.
    """
    report = hub_cache.verify_snapshot(
        str(local_repo_path(model_id)), revision, deep=deep
    )
    if use_cli:
        report["hub_cli"] = _hf_cache_verify(model_id, report.get("revision"))
        if report["hub_cli"].get("state") == hub_cache.STATE_PARTIAL:
            report["state"] = hub_cache.STATE_PARTIAL
            report["reason"] = report["hub_cli"].get("reason") or report["reason"]
    return report


def _hf_cache_verify(model_id: str, revision: str | None) -> dict[str, Any]:
    """Cross-check a local entry against the hub with its own CLI.

    Returns a small verdict dict. ``unavailable`` is not a failure: the CLI is
    an optional extra check, and the manifest walk stands on its own.
    """
    if shutil.which(HF_CLI) is None:
        return {"state": "unavailable", "reason": f"{HF_CLI} is not on PATH"}
    argv = [
        HF_CLI,
        "cache",
        "verify",
        model_id,
        "--cache-dir",
        str(hub_dir()),
        "--json",
    ]
    if revision:
        argv += ["--revision", revision]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=HF_CLI_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "unavailable", "reason": str(exc)[:500]}
    if result.returncode != 0:
        return {
            "state": hub_cache.STATE_PARTIAL,
            "reason": (
                result.stderr or result.stdout or "hf cache verify failed"
            ).strip()[:500],
        }
    return {"state": hub_cache.STATE_VERIFIED, "reason": (result.stdout or "").strip()}


def _remote_verify_command(
    repo: str,
    commit: str,
    *,
    require_manifest: bool,
    deep: bool,
    marker: dict[str, Any] | None = None,
) -> str:
    """The verifier invocation to run on a node."""
    args = [
        REMOTE_PYTHON,
        _remote_helper_path(),
        "verify",
        "--repo",
        repo,
        "--revision",
        commit,
    ]
    if require_manifest:
        args.append("--require-manifest")
    if deep:
        args.append("--deep")
    if marker is not None:
        args.extend(["--write-marker", json.dumps(marker, sort_keys=True)])
    return " ".join(_q(a) for a in args)


def _publish_command(staging: str, final: str) -> str:
    """Swap a verified staging directory into place with one rename.

    The old entry is moved aside first and deleted afterwards, so at no moment
    is there a half-written entry at the published path: a reader sees either
    the previous copy or the new one.
    """
    replaced = f"{final}.sp-replaced"
    return (
        f"set -e; rm -rf {_q(replaced)}; "
        f"if [ -e {_q(final)} ]; then mv {_q(final)} {_q(replaced)}; fi; "
        f"mv {_q(staging)} {_q(final)}; rm -rf {_q(replaced)}"
    )


def _parse_report(stdout: str) -> dict[str, Any] | None:
    """Read the verifier's JSON report out of a remote command's stdout."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "state" in parsed:
            return parsed
    return None


def _remote_bytes(ssh: SSHClient, node: str, path: str) -> int:
    """Apparent bytes currently under ``path`` on ``node``. 0 when unknown.

    Asked of the shipped verifier rather than of ``du``, whose ``-b`` is
    GNU-only and whose default answer is block usage rather than the byte count
    a progress bar has to compare against the manifest.
    """
    command = " ".join(
        _q(part)
        for part in (REMOTE_PYTHON, _remote_helper_path(), "du", "--path", path)
    )
    try:
        result = ssh.exec(node, command, timeout=60)
    except (SSHError, OSError):
        return 0
    for line in reversed((result.stdout or "").strip().splitlines()):
        try:
            parsed = json.loads(line.strip())
        except ValueError:
            continue
        if isinstance(parsed, dict) and "bytes" in parsed:
            return int(parsed["bytes"])
    return 0


class _ProgressPoller:
    """Reports real bytes on the node against the bytes the manifest expects.

    A hundred-gigabyte transfer takes hours, and an operator watching it needs
    to know it is moving and roughly when it ends — which a spinner cannot say.
    The poll is a ``du`` over the multiplexed connection the transfer is
    already using, so it costs nothing beside the transfer itself.
    """

    def __init__(
        self,
        ssh: SSHClient,
        node: str,
        path: str,
        model_id: str,
        bytes_total: int,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
        interval: float | None = None,
        host: str = "",
    ):
        self._ssh = ssh
        self._node = node
        # Which node this is about, and which address to ask, are two things:
        # the poll goes over the transfer's own connection (the fabric, when
        # there is one) while every event still names the node the operator
        # registered.
        self._host = host or node
        self._path = path
        self._model = model_id
        self._total = bytes_total
        self._on_progress = on_progress
        # Read at construction, not at import, so a test can shorten it.
        self._interval = (
            REPLICATION_POLL_INTERVAL if interval is None else max(0.01, interval)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.bytes_done = 0

    def __enter__(self) -> _ProgressPoller:
        self._thread = threading.Thread(
            target=self._run, name=f"model-repl-{self._node}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            done = _remote_bytes(self._ssh, self._host, self._path)
            if done <= 0:
                continue
            self.bytes_done = done
            update = {
                "model": self._model,
                "node": self._node,
                "bytes_done": done,
                "bytes_total": self._total,
            }
            if self._on_progress is not None:
                self._on_progress(dict(update))
            publish_event(EVENT_REPLICATION_PROGRESS, self._model, update)


def replicate_to_nodes(
    model_id: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = 3600,
    client: SSHClient | None = None,
    revision: str | None = None,
    deep: bool = False,
    force: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Replicate a model's cache entry to each node, verified before publish.

    Per node: stage, transfer, verify on the node, then one rename to publish.
    A node that already holds a verified copy of the same commit is skipped, so
    calling this twice costs one SSH round trip rather than a re-transfer, and
    a run interrupted halfway resumes from the staging directory it left.

    The HuggingFace token never leaves the control node.  The control node
    downloads with it once; nodes receive files and are handed no credential at
    all, which is what lets a gated model resolve on a worker — the hub cache
    is consulted before the network, and :func:`worker_env` then closes the
    network off entirely.

    Args:
        model_id: Model whose cache entry is replicated.
        nodes: Node addresses to replicate to.
        ssh_user: SSH login, when the default is not right.
        timeout: Seconds allowed for one node's transfer.
        client: SSH transport (tests inject a double).
        revision: Commit or ref; ``None`` resolves ``refs/main``.
        deep: Hash every file on the node as well as checking sizes.
        force: Re-transfer even to a node that already verifies.
        on_progress: Called with a progress dict as bytes land on each node.

    Returns:
        A result dict with one entry per node carrying its state, byte counts
        and, when it did not succeed, what is missing.

    Raises:
        ValueError: The model is not cached locally, no nodes were given, or
            the local copy does not itself verify — replicating a broken source
            only spreads it.
    """
    repo_path = local_repo_path(model_id)
    if not repo_path.is_dir():
        raise ValueError(f"Model not in local cache: {model_id}")
    if not nodes:
        raise ValueError("No nodes specified")

    source = hub_cache.verify_snapshot(str(repo_path), revision, deep=deep)
    if source["state"] != hub_cache.STATE_VERIFIED:
        raise ValueError(
            f"Local copy of {model_id} is {source['state']}: {source['reason']}"
        )
    commit = str(source["revision"])
    # Demand of the replica exactly the proof we hold of the source: when the
    # local entry carries a manifest the replica must match it, and when it
    # does not, no copy of it could ever produce one.
    require_manifest = source["evidence"] in (
        hub_cache.EVIDENCE_MANIFEST,
        hub_cache.EVIDENCE_HASHES,
    )
    # Two different numbers, and conflating them is how a progress bar ends up
    # stuck at 103%. ``bytes_total`` is what will land on the node — the whole
    # entry, manifest and refs included — and is the denominator progress is
    # measured against. ``manifest_bytes`` is what the revision's files weigh,
    # and is what verification counts.
    bytes_total = int(hub_cache.tree_bytes(str(repo_path))["bytes"])
    manifest_bytes = int(source["bytes_expected"])

    ssh = client or _make_ssh_client(ssh_user)
    final_dir = f"{hub_dir()}/{repo_dir_name(model_id)}"
    staging_dir = f"{_staging_root()}/{repo_dir_name(model_id)}"
    # The node is shipped exactly what is here, which for a filtered download
    # is the subset that was asked for — so the marker it verifies against has
    # to name the same filter, or the node counts files nobody fetched.
    marker = hub_cache.marker_payload(
        model_id,
        commit,
        source,
        source=_control_hostname(),
        allow_patterns=source.get("allow_patterns"),
    )

    def _one(node: str) -> dict[str, Any]:
        started = time.monotonic()
        # Which address the bytes travel over is decided here, once per node,
        # and reported: a 22 GB model over a Wi-Fi management NIC is fifteen
        # minutes and over the ConnectX fabric it is under one, and an
        # operator watching the slow one deserves to be told which they got.
        route = _transfer_route(node)
        publish_event(
            EVENT_REPLICATION_STARTED,
            model_id,
            {
                "model": model_id,
                "node": node,
                "bytes_total": bytes_total,
                "transfer_address": route.address,
            },
        )
        entry = _replicate_one(
            ssh=ssh,
            node=node,
            route=route,
            model_id=model_id,
            local_repo=repo_path,
            final_dir=final_dir,
            staging_dir=staging_dir,
            commit=commit,
            marker=marker,
            bytes_total=bytes_total,
            require_manifest=require_manifest,
            deep=deep,
            force=force,
            timeout=timeout,
            on_progress=on_progress,
        )
        entry["duration_s"] = round(time.monotonic() - started, 2)
        publish_event(
            EVENT_REPLICATION_VERIFIED if entry["ok"] else EVENT_REPLICATION_FAILED,
            model_id,
            dict(entry),
        )
        return entry

    with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
        results = list(pool.map(_one, nodes))

    return {
        "model": model_id,
        "path": str(repo_path),
        "revision": commit,
        "bytes_total": bytes_total,
        "manifest_bytes": manifest_bytes,
        "local": source,
        "results": results,
        "ok": all(r["ok"] for r in results),
    }


def _transfer_route(node: str) -> Any:
    """Where this node's bytes should go, through the switch.

    A thin wrapper so the decision is one name in this module and a test can
    replace it without reaching into the node-service package.
    """
    from spark_pulse import tools

    return tools.node_service.transfer_route(node)


def _control_hostname() -> str:
    """Best-effort name of this control node, recorded in the marker."""
    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover — defensive
        return ""


def _node_result(node: str, **fields: Any) -> dict[str, Any]:
    """A per-node result with every key present, whatever happened."""
    base: dict[str, Any] = {
        "node": node,
        "ok": False,
        "state": hub_cache.STATE_ABSENT,
        "error": None,
        "reason": "",
        "revision": None,
        # Bytes on the node's disk, against the bytes the whole entry weighs.
        "bytes_done": 0,
        "bytes_total": 0,
        # Bytes the verification actually accounted for against the manifest.
        "bytes_verified": 0,
        "missing": [],
        "missing_count": 0,
        "verified_at": None,
        "published": False,
        "skipped": False,
        # Stated rather than implied: a node is never handed a hub credential.
        "token_sent": False,
        # Which address the bytes actually travelled over, and why that one.
        # Empty until a route has been chosen.
        "transfer_address": "",
        "transfer_via_fabric": False,
        "transfer_reason": "",
    }
    base.update(fields)
    return base


def _replicate_one(
    *,
    ssh: SSHClient,
    node: str,
    route: Any = None,
    model_id: str,
    local_repo: Path,
    final_dir: str,
    staging_dir: str,
    commit: str,
    marker: dict[str, Any],
    bytes_total: int,
    require_manifest: bool,
    deep: bool,
    force: bool,
    timeout: int,
    on_progress: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """Stage, transfer, verify and publish one node's replica.

    ``node`` is the address the node is registered at — what every event and
    every result is keyed on. ``route`` says which address the bytes go over,
    which is the fabric's when there is one. They are the same machine and two
    different links.
    """
    route = route if route is not None else _transfer_route(node)
    host = route.address
    result = _node_result(
        node,
        revision=commit,
        bytes_total=bytes_total,
        transfer_address=host,
        transfer_via_fabric=route.via_fabric,
        transfer_reason=route.reason,
    )
    try:
        prepared = ssh.exec(
            host,
            f"mkdir -p {_q(_staging_root())}",
            timeout=CONTROL_COMMAND_TIMEOUT,
        )
        if not prepared.ok:
            result["error"] = (
                prepared.stderr or prepared.stdout or "mkdir failed"
            ).strip()[:500]
            result["reason"] = "could not create the staging directory"
            return result
        ssh.copy(
            str(_helper_source()),
            host,
            _remote_helper_path(),
            timeout=CONTROL_COMMAND_TIMEOUT,
        )

        if not force:
            already = _remote_verify(
                ssh,
                host,
                final_dir,
                commit,
                require_manifest=require_manifest,
                deep=False,
            )
            if already is not None and already["state"] == hub_cache.STATE_VERIFIED:
                return _node_result(
                    node,
                    ok=True,
                    skipped=True,
                    published=True,
                    state=already["state"],
                    reason="node already holds a verified copy of this revision",
                    revision=commit,
                    bytes_done=bytes_total,
                    bytes_total=bytes_total,
                    bytes_verified=int(already.get("bytes_present") or 0),
                    verified_at=already.get("verified_at"),
                    transfer_address=host,
                    transfer_via_fabric=route.via_fabric,
                    transfer_reason=route.reason,
                )

        with _ProgressPoller(
            ssh,
            node,
            staging_dir,
            model_id,
            bytes_total,
            on_progress,
            host=host,
        ) as poller:
            # One rsync run for the whole entry — blobs, snapshots, refs and
            # trees — with symlinks intact, resumable and uncompressed. See
            # OpenSSHClient.copy_dir for the flags and why each is there.
            ssh.copy_dir(str(local_repo), host, staging_dir, timeout=timeout)
        result["bytes_done"] = poller.bytes_done or _remote_bytes(
            ssh, host, staging_dir
        )

        report = _remote_verify(
            ssh,
            host,
            staging_dir,
            commit,
            require_manifest=require_manifest,
            deep=deep,
            marker=marker,
        )
        if report is None:
            result["error"] = "the node did not return a verification report"
            result["reason"] = "verification could not be run on the node"
            return result
        result.update(
            {
                "state": report["state"],
                "reason": report.get("reason", ""),
                "missing": report.get("missing") or [],
                "missing_count": int(report.get("missing_count") or 0),
                "bytes_verified": int(report.get("bytes_present") or 0),
            }
        )
        if report["state"] != hub_cache.STATE_VERIFIED:
            result["error"] = f"verification failed on {node}: {report.get('reason')}"
            # The staging directory is deliberately left in place: the next run
            # resumes from it instead of starting the transfer over.
            return result

        published = ssh.exec(
            host,
            _publish_command(staging_dir, final_dir),
            timeout=CONTROL_COMMAND_TIMEOUT,
        )
        if not published.ok:
            result["error"] = (
                published.stderr or published.stdout or "publish failed"
            ).strip()[:500]
            result["reason"] = "verified, but the rename into place failed"
            return result
        result.update(
            {
                "ok": True,
                "published": True,
                "verified_at": report.get("verified_at")
                or (report.get("marker") or {}).get("verified_at"),
            }
        )
        return result
    except (SSHError, RuntimeError, OSError) as exc:
        result["error"] = str(exc)[:500]
        result["reason"] = "transport failure"
        return result


def _remote_verify(
    ssh: SSHClient,
    node: str,
    repo: str,
    commit: str,
    *,
    require_manifest: bool,
    deep: bool,
    marker: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run the shipped verifier on a node and return its report."""
    command = _remote_verify_command(
        repo,
        commit,
        require_manifest=require_manifest,
        deep=deep,
        marker=marker,
    )
    result = ssh.exec(node, command, timeout=CONTROL_COMMAND_TIMEOUT)
    return _parse_report(result.stdout)


def _node_services(services: Any | None = None) -> Callable[[Any], Any]:
    """The resolver every node — including this one — is reached through."""
    if services is not None:
        return services
    from spark_pulse import tools

    return tools.node_service.NodeServices()


def presence(
    model_id: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = CONTROL_COMMAND_TIMEOUT,
    client: SSHClient | None = None,
    revision: str | None = None,
    deep: bool = False,
    services: Any | None = None,
) -> dict[str, Any]:
    """Report, per node, whether the model is absent, partial or verified.

    The old check ran ``test -d …/snapshots`` and called a hit "present". That
    directory exists after a transfer that copied no symlinks, after one that
    copied symlinks but no blobs, and after one that truncated every file, so
    "present" meant nothing.

    What replaced it shipped ``hub_cache.py`` to each node over SSH and ran it
    there — a second copy of the verifier on a machine that may not have the
    interpreter for it, and an SSH login per question. The node now *lists* its
    snapshot through its own agent, and the verdict is reached here, against
    the manifest this side already holds. One verifier, one transport.
    """
    repo_path = local_repo_path(model_id)
    local_report = hub_cache.verify_snapshot(str(repo_path), revision, deep=deep)
    commit = local_report.get("revision")
    require_manifest = local_report["evidence"] in (
        hub_cache.EVIDENCE_MANIFEST,
        hub_cache.EVIDENCE_HASHES,
    )
    manifest = hub_cache.read_manifest(str(repo_path), str(commit)) if commit else None
    # What a filtered download fetched here is what replication shipped there.
    # The node's own marker is not in the listing — it sits beside the
    # snapshot, not inside it — so the filter travels from this side.
    allow_patterns = local_report.get("allow_patterns")
    remote_dir = f"{hub_dir()}/{repo_dir_name(model_id)}"
    resolve = _node_services(services)

    def _one(node: str) -> dict[str, Any]:
        from spark_pulse import tools

        try:
            service = resolve(
                tools.node_service.node_for(node, ssh_user=ssh_user or "")
            )
            listing = service.list_snapshot(remote_dir, str(commit or ""), deep)
        except Exception as exc:  # noqa: BLE001 — a node that cannot be asked
            return _presence_entry(node, None, error=str(exc)[:500])
        report = hub_cache.verify_listing(
            _listed_files(listing),
            commit=listing.revision or str(commit or ""),
            present=bool(listing.present),
            manifest=manifest,
            deep=deep,
            require_manifest=require_manifest,
            allow_patterns=allow_patterns,
        )
        return _presence_entry(node, report)

    results: list[dict[str, Any]] = []
    if nodes:
        with ThreadPoolExecutor(max_workers=max(1, len(nodes))) as pool:
            results = list(pool.map(_one, nodes))
    return {
        "model": model_id,
        "revision": commit,
        # ``local`` stays a bool for callers that only ask "is it here", but it
        # is now the verified verdict rather than a directory listing.
        "local": local_report["state"] == hub_cache.STATE_VERIFIED,
        "local_state": local_report["state"],
        # A deliberately narrowed download is verified, and says which files
        # it was narrowed to — never "partial" for the ones nobody asked for.
        "local_filtered": bool(local_report.get("filtered")),
        "local_report": local_report,
        "nodes": results,
    }


def _listed_files(listing: Any) -> list[dict[str, Any]]:
    """A ``SnapshotListing`` as the plain dicts ``hub_cache`` reads.

    ``hub_cache`` imports nothing from this package and knows nothing about
    protobuf — that is what lets it be a standalone script — so the shape
    crosses here rather than there.
    """
    return [
        {
            "path": entry.path,
            "size": entry.size_bytes,
            "sha256": entry.sha256,
            "is_symlink": entry.is_symlink,
            "resolved": entry.resolved,
        }
        for entry in listing.files
    ]


def _presence_entry(
    node: str, report: dict[str, Any] | None, error: str | None = None
) -> dict[str, Any]:
    """One node's presence row, in the three-state shape."""
    if report is None:
        return {
            "node": node,
            "state": hub_cache.STATE_ABSENT,
            "filtered": False,
            "present": False,
            "reason": "no verification report" if error is None else "",
            "revision": None,
            "bytes_present": 0,
            "bytes_expected": 0,
            "files_present": 0,
            "files_expected": 0,
            "missing": [],
            "missing_count": 0,
            "verified_at": None,
            "error": error,
        }
    state = report.get("state", hub_cache.STATE_ABSENT)
    return {
        "node": node,
        "state": state,
        "filtered": bool(report.get("filtered")),
        # "Verified", never "a directory exists" — which is what the check
        # this replaced actually tested.
        "present": state == hub_cache.STATE_VERIFIED,
        "reason": report.get("reason", ""),
        "revision": report.get("revision"),
        "bytes_present": int(report.get("bytes_present") or 0),
        "bytes_expected": int(report.get("bytes_expected") or 0),
        "files_present": int(report.get("files_present") or 0),
        "files_expected": int(report.get("files_expected") or 0),
        "missing": list(report.get("missing") or []),
        "missing_count": int(report.get("missing_count") or 0),
        # When the check itself ran, for a node that passed it. This used to
        # come from a marker a replication wrote on the node, which said when
        # some earlier transfer proved the copy; the check now runs on every
        # ask, so the answer is the check that just happened.
        "verified_at": report.get("verified_at")
        or (report.get("checked_at") if state == hub_cache.STATE_VERIFIED else None),
        "error": error,
    }


# ── Worker credentials ───────────────────────────────────────────────────────

#: What a worker container is given once its weights are replicated. The token
#: is absent by construction and the hub is switched off, so a worker that is
#: somehow missing a file fails loudly instead of quietly downloading it again
#: over the uplink — with no credential, from a gated repo, on every node.
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}

#: Credentials that must never reach a worker node.
TOKEN_ENV_KEYS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN")


def worker_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Strip the hub token from a worker's environment and pin it offline.

    The control node holds the token and downloads once; a node holds files.
    A gated model resolves out of a local cache with no credential at all,
    which is the property that makes fetch-once distribution work end to end —
    so the token has no reason to be on a worker, and being offline means it
    can never be asked for one.
    """
    out = dict(env or {})
    for key in TOKEN_ENV_KEYS:
        out.pop(key, None)
    out.update(OFFLINE_ENV)
    return out


# ── Deletion ─────────────────────────────────────────────────────────────────


def models_in_use() -> dict[str, list[str]]:
    """Map lowercased model id -> deployment ids of running/pending deployments."""
    in_use: dict[str, list[str]] = {}
    try:
        from spark_pulse import tools

        deployments = tools.deployment_records.load()
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


def delete_model(
    model_id: str,
    nodes: list[str] | None = None,
    revision: str | None = None,
    services: Any | None = None,
) -> dict[str, Any]:
    """Delete a cached model, here and on whichever nodes were named.

    A 26 GB model replicated to four Sparks used to be deleted from one of
    them, and the page then said it was gone. Every node is asked through its
    own agent — including this one, which is why there is no ``shutil.rmtree``
    left here — and each answers for itself: removed or not, and how much it
    freed.

    Naming no revision takes the whole repository, which is what an operator
    clearing a model means. Naming one leaves the blobs alone, because they
    are shared with the revisions that stay.
    """
    from spark_pulse import tools

    users = models_in_use().get(model_id.lower(), [])
    if users:
        raise ValueError(
            f"Model {model_id} is in use by running deployment(s): {', '.join(users)}"
        )
    repo_path = hub_dir() / repo_dir_name(model_id)
    local_present = repo_path.is_dir()
    if not local_present and not nodes:
        raise ValueError(f"Model not in local cache: {model_id}")

    resolve = _node_services(services)
    targets: list[tuple[str, Any]] = [("", tools.node_service.control_node())]
    for address in nodes or []:
        if tools.node_service.is_local_address(address):
            continue
        targets.append((address, tools.node_service.node_for(address)))

    def _one(target: tuple[str, Any]) -> dict[str, Any]:
        address, node = target
        try:
            removal = resolve(node).remove_snapshot(str(repo_path), revision or "")
        except Exception as exc:  # noqa: BLE001 — a node that cannot be asked
            return {
                "node": address,
                "removed": False,
                "freed_bytes": 0,
                "error": str(exc)[:500],
            }
        return {
            "node": address,
            "removed": bool(removal.removed),
            "freed_bytes": int(removal.freed_bytes),
            "error": None,
        }

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        answers = list(pool.map(_one, targets))

    result = {
        "deleted": model_id,
        "path": str(repo_path),
        "freed_bytes": sum(a["freed_bytes"] for a in answers),
        "nodes": answers,
    }
    publish_event(EVENT_DELETED, model_id, result)
    return result
