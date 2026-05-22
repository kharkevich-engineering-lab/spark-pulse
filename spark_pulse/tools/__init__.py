"""Tool module factory — loads real or mock tools based on SIMULATION_MODE.

Set SIMULATION_MODE=1 environment variable to use mock tools.
Default: 0 (real tools — production mode).
"""

from __future__ import annotations

import os

_sim_mode = os.environ.get("SIMULATION_MODE", "0") == "1"

if _sim_mode:
    from spark_pulse.mock import (
        system as system,
        cache as cache,
        recipes as recipes,
        deployments as deployments,
    )
else:
    from spark_pulse.tools import (
        system,
        cache,
        recipes,
        deployments,
    )


def is_simulation() -> bool:
    """Return whether simulation mode is active."""
    return _sim_mode
