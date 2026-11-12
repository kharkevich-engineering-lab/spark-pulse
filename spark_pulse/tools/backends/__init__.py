"""Run backends: where a run actually runs, behind one protocol.

:func:`backend_for` is the one choice. It always answers Docker today; the
Kubernetes backend (#175) is the first thing that will make it choose.

Simulation does not mock a backend. It keeps swapping at the agent transport
(``MockDockerService`` under ``tools.native_runtime``), as ``mock/cache.py``
does, so the backend code that runs in simulation is the code that runs on a
cluster. That is why this package is real-only in ``tools/__init__``.
"""

from __future__ import annotations

from typing import Any

from spark_pulse.tools.backends.base import RunBackend as RunBackend
from spark_pulse.tools.backends.docker import DockerBackend as DockerBackend

_DOCKER = DockerBackend()


def backend_for(target: dict[str, Any] | str | None = None) -> RunBackend:
    """The backend that owns ``target``: a run's record, its id, or a request.

    Docker, whatever is passed. The parameter is the seam: the record's
    ``runtime`` field stays a note about how a run was made, and the choice
    will be made from what a backend can do and what the nodes are, not from
    a string somebody can edit.
    """
    del target  # one backend today; the argument is where the choice will go
    return _DOCKER
