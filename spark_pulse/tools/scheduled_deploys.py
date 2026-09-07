"""Deployments waiting on a model that is still downloading.

An operator who deploys a recipe whose model is not on disk used to get a 400
naming the model and telling them to download it first. Everything they needed
existed — the catalogue, the download endpoint, the progress stream — and none
of it was joined up, so the answer to "the model is missing" was a sentence
rather than an offer.

This is the joining-up: the intent to deploy *once the model arrives* is
recorded, and something acts on it when the download finishes.

**Why the intent lives here and not in the browser.** The obvious
implementation is a page that watches the download and fires the deploy when
it completes, and it is wrong for three reasons an operator will meet on the
first day: closing the tab loses a deployment they asked for; the Models page
is a different page and would have no idea a download had a deployment behind
it; and a 20 GB download outlives any particular browser session. A record
here is visible to every client, survives a restart, and can be cancelled from
wherever the operator happens to be looking.

**Reconciled, not only triggered.** The download thread calls a hook when it
finishes, which is the fast path. But a control plane that restarts mid-
download would never see that call, so :func:`reconcile` runs at startup and
settles every record against what is actually on disk now. The hook is an
optimisation; the reconcile is the guarantee — the same shape the deployment
records already use against container labels.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, String, select, update
from sqlalchemy.orm import Mapped, mapped_column

from spark_pulse.db import Base, session_scope

logger = logging.getLogger(__name__)

__all__ = [
    "ScheduledDeploy",
    "STATUS_WAITING",
    "STATUS_DEPLOYING",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "schedule",
    "get",
    "listing",
    "for_model",
    "cancel",
    "on_download_finished",
    "reconcile",
]

#: Waiting for the model to arrive.
STATUS_WAITING = "waiting"
#: The model arrived and the deployment is being created.
STATUS_DEPLOYING = "deploying"
#: The deployment was created. Terminal.
STATUS_DONE = "done"
#: The download failed, or the deployment did. Terminal, with ``error`` set.
STATUS_FAILED = "failed"
#: The operator called it off. Terminal.
STATUS_CANCELLED = "cancelled"

_ACTIVE = (STATUS_WAITING, STATUS_DEPLOYING)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScheduledDeploy(Base):
    """One deployment waiting on one model.

    ``request`` is the entire create body, verbatim. Storing the request rather
    than its parts is deliberate: the deploy that eventually runs must be the
    one the operator asked for, and a schema that names each field separately
    would silently drop whatever was added to the create API afterwards.
    """

    __tablename__ = "scheduled_deploys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: The model being waited on, exactly as the plan resolved it.
    model: Mapped[str] = mapped_column(String(512), default="", index=True)
    #: The download this is waiting on, so cancelling can reach it.
    download_job_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default=STATUS_WAITING, index=True)
    #: Operator-facing name of the deployment that will be created.
    name: Mapped[str] = mapped_column(String(255), default="")
    recipe_id: Mapped[str] = mapped_column(String(255), default="")
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    #: Set once the deployment exists, so the UI can link to it.
    deployment_id: Mapped[str] = mapped_column(String(128), default="")
    error: Mapped[str] = mapped_column(String(2048), default="")
    created_at: Mapped[str] = mapped_column(String(64), default="")
    finished_at: Mapped[str] = mapped_column(String(64), default="")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "download_job_id": self.download_job_id,
            "status": self.status,
            "name": self.name,
            "recipe_id": self.recipe_id,
            "request": dict(self.request or {}),
            "deployment_id": self.deployment_id,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


def schedule(
    *, model: str, download_job_id: str, request: dict[str, Any]
) -> dict[str, Any]:
    """Record that ``request`` should be deployed once ``model`` is here."""
    entry = ScheduledDeploy(
        id=uuid.uuid4().hex[:12],
        model=model,
        download_job_id=download_job_id,
        status=STATUS_WAITING,
        name=str(request.get("name") or ""),
        recipe_id=str(request.get("recipe_id") or ""),
        request=dict(request),
        created_at=_now(),
    )
    with session_scope() as db:
        db.add(entry)
        stored = entry.to_dict()
    logger.info(
        "deployment %r scheduled behind the download of %s (job %s)",
        stored["name"],
        model,
        download_job_id,
    )
    return stored


def get(entry_id: str) -> dict[str, Any] | None:
    with session_scope() as db:
        row = db.get(ScheduledDeploy, entry_id)
        return row.to_dict() if row is not None else None


def listing(*, active_only: bool = False) -> list[dict[str, Any]]:
    """Every scheduled deploy, newest first."""
    with session_scope() as db:
        rows = list(db.execute(select(ScheduledDeploy)).scalars())
    entries = [row.to_dict() for row in rows]
    if active_only:
        entries = [e for e in entries if e["status"] in _ACTIVE]
    return sorted(entries, key=lambda e: e["created_at"], reverse=True)


def for_model(model: str) -> list[dict[str, Any]]:
    """The active schedules waiting on one model.

    What the Models page asks so a download can say what it is *for* — a
    progress bar with no purpose attached is the thing this feature exists to
    stop showing.
    """
    return [e for e in listing(active_only=True) if e["model"] == model]


def cancel(entry_id: str, *, cancel_download: bool = True) -> dict[str, Any] | None:
    """Call off a scheduled deploy, and by default the download behind it.

    ``cancel_download`` is a choice the caller has to make rather than an
    assumption, because the two are not the same wish: "do not deploy this"
    and "stop fetching 20 GB" usually travel together, but an operator who
    wants the model anyway must be able to keep it.
    """
    from spark_pulse import tools

    with session_scope() as db:
        row = db.get(ScheduledDeploy, entry_id)
        if row is None:
            return None
        if row.status not in _ACTIVE:
            return row.to_dict()
        row.status = STATUS_CANCELLED
        row.finished_at = _now()
        stored = row.to_dict()

    if cancel_download and stored["download_job_id"]:
        # Only when nothing else is waiting on the same download: two
        # deployments behind one model is a normal thing to ask for, and the
        # first cancellation must not take the second one's bytes with it.
        others = [
            e
            for e in for_model(stored["model"])
            if e["download_job_id"] == stored["download_job_id"]
        ]
        if not others:
            try:
                tools.models.cancel_download(stored["download_job_id"])
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning(
                    "could not cancel download %s: %s", stored["download_job_id"], exc
                )
    return stored


def _finish(entry_id: str, status: str, **fields: Any) -> None:
    with session_scope() as db:
        row = db.get(ScheduledDeploy, entry_id)
        if row is None:
            return
        row.status = status
        row.finished_at = _now()
        for key, value in fields.items():
            setattr(row, key, value)


def _claim(entry_id: str) -> bool:
    """Take this entry for deploying. False if somebody already had it.

    A conditional UPDATE rather than a read followed by a write, because the
    read-then-write has a window: two control planes sharing one PostgreSQL
    both see ``waiting``, both proceed, and the operator gets two containers
    for one deployment they asked for once. The database decides instead —
    exactly one statement matches the row while it still says ``waiting``, and
    ``rowcount`` says whether it was ours.
    """
    with session_scope() as db:
        result = db.execute(
            update(ScheduledDeploy)
            .where(
                ScheduledDeploy.id == entry_id,
                ScheduledDeploy.status == STATUS_WAITING,
            )
            .values(status=STATUS_DEPLOYING)
        )
        return bool(result.rowcount)


def _deploy(entry: dict[str, Any]) -> None:
    """Create the deployment this entry was holding."""
    from spark_pulse import tools

    if not _claim(entry["id"]):
        return  # cancelled, already settled, or another instance took it

    request = dict(entry["request"])
    try:
        created = tools.deploy_dispatch.create_deployment(
            recipe_id=request.get("recipe_id", ""),
            name=request.get("name") or request.get("recipe_id", ""),
            params=request.get("params") or {},
            nodes=request.get("nodes"),
            engine=request.get("engine"),
            variant=request.get("variant"),
            model=request.get("model"),
            extra_args=request.get("extra_args") or [],
            allow_missing_model=False,
        )
    except Exception as exc:  # noqa: BLE001 — any failure belongs on the record
        logger.warning("scheduled deploy %s failed: %s", entry["id"], exc)
        _finish(entry["id"], STATUS_FAILED, error=str(exc)[:2000])
        return
    deployment_id = str((created or {}).get("id") or "")
    _finish(entry["id"], STATUS_DONE, deployment_id=deployment_id)
    logger.info("scheduled deploy %s created deployment %s", entry["id"], deployment_id)


def _spawn_deploy(entry: dict[str, Any]) -> None:
    """Deploy on a thread of its own.

    Used by :func:`reconcile`, which runs during application startup: creating
    a deployment pulls an image and starts containers, and doing that inline
    would hold the control plane's startup — and every request to it — for as
    long as the pull takes. The download thread's hook needs none of this; it
    is already off the event loop and has nothing left to hold up.
    """
    threading.Thread(
        target=_deploy,
        args=(entry,),
        name=f"scheduled-deploy-{entry['id']}",
        daemon=True,
    ).start()


def on_download_finished(job: dict[str, Any]) -> None:
    """Hook called by the download thread when a job reaches a terminal state.

    Runs on that thread on purpose: it is the thread that just finished the
    work, it is already a daemon doing something long, and doing the deploy
    inline means the model cannot be deleted between "arrived" and "used".
    """
    model = str(job.get("model") or "")
    status = str(job.get("status") or "")
    if not model:
        return
    for entry in for_model(model):
        if status == "completed":
            # Any completed download of this model will do, whichever job
            # fetched it. What the entry is waiting for is the model, and
            # refusing to notice it because it arrived by another route would
            # leave a deployment queued behind a job that has already been
            # superseded.
            _deploy(entry)
        elif status in ("failed", "cancelled"):
            # A failure, in contrast, only settles the entries that were
            # actually behind *this* job: another download's collapse says
            # nothing about the one this deployment is waiting on.
            if entry["download_job_id"] != job.get("id"):
                continue
            _finish(
                entry["id"],
                STATUS_FAILED if status == "failed" else STATUS_CANCELLED,
                error=str(job.get("error") or "")[:2000]
                or f"the download was {status}",
            )


def reconcile() -> int:
    """Settle every waiting record against what is on disk now.

    Returns how many were acted on. The deploys themselves are dispatched to
    their own threads — see :func:`_spawn_deploy` — so this returns promptly
    even when it found work.

    The hook above is the fast path and a restart never sees it, so this runs
    at startup: a model that arrived while the control plane was down still
    gets its deployment, and a schedule whose download died with the process is
    reported rather than left waiting for ever.
    """
    from spark_pulse import tools

    settled = 0
    for entry in listing(active_only=True):
        if entry["status"] == STATUS_DEPLOYING:
            # Interrupted between "model arrived" and "deployment created".
            # Reported rather than retried: the create may have got far enough
            # to start a container, and doing it twice is worse than saying so.
            _finish(
                entry["id"],
                STATUS_FAILED,
                error="the control plane restarted while this was deploying; "
                "check whether the deployment exists and re-issue it if not",
            )
            settled += 1
            continue
        try:
            present = tools.models.get_model(entry["model"]) is not None
        except Exception:  # pragma: no cover - catalogue is best effort
            continue
        if present:
            _spawn_deploy(entry)
            settled += 1
            continue
        job = None
        if entry["download_job_id"]:
            try:
                job = tools.models.get_download(entry["download_job_id"])
            except Exception:  # pragma: no cover
                job = None
        if job is None or job.get("status") in ("failed", "cancelled"):
            _finish(
                entry["id"],
                STATUS_FAILED,
                error="the download this was waiting on is no longer running",
            )
            settled += 1
    return settled
