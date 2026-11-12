"""Whether a run's engine is ready: the question asked of its endpoint.

Split from :mod:`native_runtime` because the question is the engine's HTTP
surface, not Docker's; a backend that runs the engine elsewhere asks it the
same way.
"""

from __future__ import annotations

import shlex
from typing import Any

from spark_pulse import tools


def _served_model_name(command: str) -> str:
    """The name a rendered command serves its model under, if it says.

    The first ``--served-model-name`` (either spelling; vLLM takes several and
    lists them all). Without one, vLLM lists the model exactly as it was
    handed — the argument after ``serve`` — and that is read too, because a
    v1 template may hand it a local path the recipe's ``model:`` never names,
    and waiting for the recipe's id there would wait out every deadline. A
    command that says neither, or does not parse, names nothing, and the plan
    falls back to the model id.
    """
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        return ""
    for index, token in enumerate(tokens):
        if token.startswith("--served-model-name="):
            return token.split("=", 1)[1].strip()
        if token == "--served-model-name" and index + 1 < len(tokens):
            if not tokens[index + 1].startswith("-"):
                return tokens[index + 1]
    for index, token in enumerate(tokens[:-1]):
        if token == "serve" and not tokens[index + 1].startswith("-"):
            return tokens[index + 1]
    return ""


def _listed_models(response: Any) -> list[str] | None:
    """The ids in an OpenAI ``/v1/models`` body, or ``None`` when it is not one."""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — not JSON is "not a listing", not an error
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None
    return [str(e.get("id")) for e in data if isinstance(e, dict) and e.get("id")]


def probe_ready(url: str, timeout: float = 3.0, model: str = "") -> bool:
    """Whether *this run's* engine answers its readiness endpoint.

    Any 200 used to do, and a port is not a server: when the plan's port was
    already held, the deploy watched somebody else's engine answer and wrote
    ``running`` over its own, which had failed to bind. So where the readiness
    path is the model listing, ``model`` is the name it has to carry — a 200
    naming another model is not ready. A body that is not a listing at all
    names nobody, and is read as before rather than refused on a technicality.

    Simulation mode has no engine to answer, so the probe succeeds — the mock
    container service is already pretending the rest of the lifecycle worked.
    """
    import httpx

    if tools.is_simulation():
        return True

    try:
        response = httpx.get(url, timeout=timeout)
    except Exception:
        return False
    if response.status_code >= 400:
        return False
    if not model:
        return True
    served = _listed_models(response)
    return served is None or model in served


def _models_answering(url: str, timeout: float = 3.0) -> list[str] | None:
    """Which models the server on ``url`` lists, for the error that names them."""
    import httpx

    if tools.is_simulation():
        return None
    try:
        response = httpx.get(url, timeout=timeout)
    except Exception:
        return None
    if response.status_code >= 400:
        return None
    return _listed_models(response)
