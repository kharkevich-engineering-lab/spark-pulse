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
import shlex
import shutil
import socket
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


def _make_ssh_client(ssh_user: str | None) -> SSHClient:
    """Build the SSH client used for distribution (overridable in tests)."""
    return OpenSSHClient(user=ssh_user or None, host_key_policy="strict")


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


def verify_local(
    model_id: str, revision: str | None = None, deep: bool = False
) -> dict[str, Any]:
    """Verify the control node's own copy of a model."""
    return hub_cache.verify_snapshot(
        str(local_repo_path(model_id)), revision, deep=deep
    )


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
    ):
        self._ssh = ssh
        self._node = node
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
            done = _remote_bytes(self._ssh, self._node, self._path)
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
    marker = hub_cache.marker_payload(
        model_id, commit, source, source=_control_hostname()
    )

    def _one(node: str) -> dict[str, Any]:
        started = time.monotonic()
        publish_event(
            EVENT_REPLICATION_STARTED,
            model_id,
            {"model": model_id, "node": node, "bytes_total": bytes_total},
        )
        entry = _replicate_one(
            ssh=ssh,
            node=node,
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
    }
    base.update(fields)
    return base


def _replicate_one(
    *,
    ssh: SSHClient,
    node: str,
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
    """Stage, transfer, verify and publish one node's replica."""
    result = _node_result(node, revision=commit, bytes_total=bytes_total)
    try:
        prepared = ssh.exec(
            node,
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
            node,
            _remote_helper_path(),
            timeout=CONTROL_COMMAND_TIMEOUT,
        )

        if not force:
            already = _remote_verify(
                ssh,
                node,
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
                )

        with _ProgressPoller(
            ssh, node, staging_dir, model_id, bytes_total, on_progress
        ) as poller:
            # One rsync run for the whole entry — blobs, snapshots, refs and
            # trees — with symlinks intact, resumable and uncompressed. See
            # OpenSSHClient.copy_dir for the flags and why each is there.
            ssh.copy_dir(str(local_repo), node, staging_dir, timeout=timeout)
        result["bytes_done"] = poller.bytes_done or _remote_bytes(
            ssh, node, staging_dir
        )

        report = _remote_verify(
            ssh,
            node,
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
            node,
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


#: The old name for the operation. Replication is what it always meant to do.
sync_to_nodes = replicate_to_nodes


def presence(
    model_id: str,
    nodes: list[str],
    ssh_user: str | None = None,
    timeout: int = CONTROL_COMMAND_TIMEOUT,
    client: SSHClient | None = None,
    revision: str | None = None,
    deep: bool = False,
) -> dict[str, Any]:
    """Report, per node, whether the model is absent, partial or verified.

    The old check ran ``test -d …/snapshots`` and called a hit "present". That
    directory exists after a transfer that copied no symlinks, after one that
    copied symlinks but no blobs, and after one that truncated every file, so
    "present" meant nothing.  This asks the node to check its own copy against
    the manifest and says which of the three it actually is, naming what is
    missing when the answer is ``partial``.
    """
    repo_path = local_repo_path(model_id)
    local_report = hub_cache.verify_snapshot(str(repo_path), revision, deep=deep)
    commit = local_report.get("revision")
    require_manifest = local_report["evidence"] in (
        hub_cache.EVIDENCE_MANIFEST,
        hub_cache.EVIDENCE_HASHES,
    )
    remote_dir = f"{hub_dir()}/{repo_dir_name(model_id)}"
    ssh = client or _make_ssh_client(ssh_user)

    def _one(node: str) -> dict[str, Any]:
        try:
            ssh.exec(node, f"mkdir -p {_q(_staging_root())}", timeout=timeout)
            ssh.copy(
                str(_helper_source()),
                node,
                _remote_helper_path(),
                timeout=timeout,
            )
            report = _remote_verify(
                ssh,
                node,
                remote_dir,
                str(commit or ""),
                require_manifest=require_manifest,
                deep=deep,
            )
        except (SSHError, RuntimeError, OSError) as exc:
            return _presence_entry(node, None, error=str(exc)[:500])
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
        "local_report": local_report,
        "nodes": results,
    }


def _presence_entry(
    node: str, report: dict[str, Any] | None, error: str | None = None
) -> dict[str, Any]:
    """One node's presence row, in the three-state shape."""
    if report is None:
        return {
            "node": node,
            "state": hub_cache.STATE_ABSENT,
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
        # Retained for callers written against the old boolean; it now means
        # "verified", never "a directory exists".
        "present": state == hub_cache.STATE_VERIFIED,
        "reason": report.get("reason", ""),
        "revision": report.get("revision"),
        "bytes_present": int(report.get("bytes_present") or 0),
        "bytes_expected": int(report.get("bytes_expected") or 0),
        "files_present": int(report.get("files_present") or 0),
        "files_expected": int(report.get("files_expected") or 0),
        "missing": list(report.get("missing") or []),
        "missing_count": int(report.get("missing_count") or 0),
        "verified_at": report.get("verified_at"),
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
