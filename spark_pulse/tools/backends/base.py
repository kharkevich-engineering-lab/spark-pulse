"""What a run backend is: the lifecycle the deployment API drives.

:mod:`tools.deploy_dispatch` is the one entry point the routers and the MCP
tools call. It used to call :mod:`tools.native_runtime` directly; it now asks
:func:`~spark_pulse.tools.backends.backend_for` which backend owns a run and
calls this protocol on it, so a backend that is not Docker (Kubernetes, #175)
plugs in here and nowhere else.

The shapes are today's, on purpose: a plan is the object ``native_runtime``
builds (the dispatcher turns it into a dict), and a run is the record dict
every caller already reads. Nothing here is new behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from spark_pulse.tools.backends.runtime_checks import RuntimeReport
    from spark_pulse.tools.intent import RunIntent


@runtime_checkable
class RunBackend(Protocol):
    """The lifecycle of a run, on whatever actually runs it."""

    #: A short name for logs and for the record's own note about how a run
    #: was made. Not a switch: nothing dispatches on it.
    name: str

    def plan(
        self,
        recipe_id: str,
        *,
        engine: str | None = None,
        variant: str | None = None,
        model: str | None = None,
        params: dict[str, Any] | None = None,
        extra_args: list[str] | None = None,
        nodes: list[str] | None = None,
        solo: bool = True,
        allow_missing_model: bool = True,
    ) -> Any:
        """Resolve everything a start would, and start nothing.

        Answers an object with ``to_dict()``, the shape the preview reads.
        """
        ...

    def realise(
        self, intent: RunIntent, services: Callable[[str], Any] | None = None
    ) -> Any:
        """How this backend runs ``intent``: a plan with ``to_dict()``.

        The intent (``tools.intent.resolve_intent``) says what the run is; this
        decides everything that depends on where it runs — the API port beside
        what the nodes already serve, where the model file sits, what is
        launched and the memory budget.
        """
        ...

    def model_location(
        self,
        intent: RunIntent,
        node: str,
        services: Callable[[str], Any],
        warnings: list[str],
    ) -> tuple[str, str]:
        """Where the engine finds the model file: ``(path, source)``.

        A hook rather than part of the intent, because the path is inside
        whatever the engine runs in — a bind mount on Docker, a volume on
        Kubernetes. ``("", "")`` for an engine handed a model id; ``source``
        is ``hf-cache`` or ``engine-download``.
        """
        ...

    def start(
        self,
        recipe_id: str,
        name: str,
        params: dict[str, Any],
        *,
        nodes: list[str] | None = None,
        engine: str | None = None,
        variant: str | None = None,
        model: str | None = None,
        extra_args: list[str] | None = None,
        allow_missing_model: bool = False,
    ) -> dict[str, Any]:
        """Plan and launch a run; answer its record as first written."""
        ...

    def status(self, run_id: str) -> dict[str, Any] | None:
        """One run as the nodes report it now, or ``None`` if it is gone."""
        ...

    def stop(self, run_id: str) -> dict[str, Any] | None:
        """Stop a run's containers and keep its record."""
        ...

    def delete(self, run_id: str) -> bool:
        """Clear a run: its containers and its record."""
        ...

    def logs(self, run_id: str, lines: int = 200) -> str:
        """The tail of a run's engine output."""
        ...

    def list_runs(self) -> list[dict[str, Any]]:
        """Every run this backend owns, reconciled against the nodes."""
        ...

    def diagnose(
        self,
        node_id: str,
        label: str = "",
        run: Callable[[str], Any] | None = None,
        facts: Any = None,
    ) -> RuntimeReport:
        """What this backend needs on ``node_id`` to run there, and what it found.

        ``node_id`` is the agent's id (the ledger's). ``facts`` is what that
        agent last reported (``NodeFacts``), when the caller holds it; else
        the backend asks the running control plane's hub. ``run`` executes one
        shell command on that node and returns its outcome, or ``None`` when
        it could not be asked; the backend declares the commands, the caller
        owns the transport. The doctor and the pre-flight both read a node's
        runtime through here (#168).
        """
        ...
