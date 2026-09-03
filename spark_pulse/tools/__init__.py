"""Tool module factory — loads real or mock tools based on SIMULATION_MODE.

Set SIMULATION_MODE=1 environment variable to use mock tools.
Default: 0 (real tools — production mode).
"""

from __future__ import annotations

import os

_sim_mode = os.environ.get("SIMULATION_MODE", "0") == "1"

# ``cluster_models`` and ``labels`` are pure data/constants with no behaviour
# to simulate, so they are real-only and intentionally absent from the mock list.
if _sim_mode:
    from spark_pulse.mock import (
        benchmarking as benchmarking,
    )
    from spark_pulse.mock import (
        cache as cache,
    )
    from spark_pulse.mock import (
        cluster as cluster,
    )
    from spark_pulse.mock import (
        cluster_health as cluster_health,
    )
    from spark_pulse.mock import (
        custom_files as custom_files,
    )
    from spark_pulse.mock import (
        custom_recipes as custom_recipes,
    )
    from spark_pulse.mock import (
        deployments as deployments,
    )
    from spark_pulse.mock import (
        discovery as discovery,
    )
    from spark_pulse.mock import (
        docker as docker,
    )
    from spark_pulse.mock import (
        events as events,
    )
    from spark_pulse.mock import (
        git_update as git_update,
    )
    from spark_pulse.mock import (
        health as health,
    )
    from spark_pulse.mock import (
        launch_script as launch_script,
    )
    from spark_pulse.mock import (
        locking as locking,
    )
    from spark_pulse.mock import (
        mods as mods,
    )
    from spark_pulse.mock import (
        network as network,
    )
    from spark_pulse.mock import (
        parallelism as parallelism,
    )
    from spark_pulse.mock import (
        ray as ray,
    )
    from spark_pulse.mock import (
        recipes as recipes,
    )
    from spark_pulse.mock import (
        reconciliation as reconciliation,
    )
    from spark_pulse.mock import (
        remote_docker as remote_docker,
    )
    from spark_pulse.mock import (
        ssh as ssh,
    )
    from spark_pulse.mock import (
        system as system,
    )
    from spark_pulse.tools import labels as labels
else:
    from spark_pulse.tools import (
        benchmarking as benchmarking,
    )
    from spark_pulse.tools import (
        cache as cache,
    )
    from spark_pulse.tools import (
        cluster as cluster,
    )
    from spark_pulse.tools import (
        cluster_health as cluster_health,
    )
    from spark_pulse.tools import (
        cluster_models as cluster_models,
    )
    from spark_pulse.tools import (
        custom_files as custom_files,
    )
    from spark_pulse.tools import (
        custom_recipes as custom_recipes,
    )
    from spark_pulse.tools import (
        deployments as deployments,
    )
    from spark_pulse.tools import (
        discovery as discovery,
    )
    from spark_pulse.tools import (
        docker as docker,
    )
    from spark_pulse.tools import (
        events as events,
    )
    from spark_pulse.tools import (
        git_update as git_update,
    )
    from spark_pulse.tools import (
        health as health,
    )
    from spark_pulse.tools import (
        labels as labels,
    )
    from spark_pulse.tools import (
        launch_script as launch_script,
    )
    from spark_pulse.tools import (
        locking as locking,
    )
    from spark_pulse.tools import (
        mods as mods,
    )
    from spark_pulse.tools import (
        network as network,
    )
    from spark_pulse.tools import (
        parallelism as parallelism,
    )
    from spark_pulse.tools import (
        ray as ray,
    )
    from spark_pulse.tools import (
        recipes as recipes,
    )
    from spark_pulse.tools import (
        reconciliation as reconciliation,
    )
    from spark_pulse.tools import (
        remote_docker as remote_docker,
    )
    from spark_pulse.tools import (
        ssh as ssh,
    )
    from spark_pulse.tools import (
        system as system,
    )


def is_simulation() -> bool:
    """Return whether simulation mode is active."""
    return _sim_mode
