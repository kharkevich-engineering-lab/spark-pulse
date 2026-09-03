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
        recipe_import as recipe_import,
        deployments as deployments,
        benchmarking as benchmarking,
        custom_files as custom_files,
        custom_recipes as custom_recipes,
        mods as mods,
        git_update as git_update,
        docker as docker,
        network as network,
        discovery as discovery,
        ssh as ssh,
        remote_docker as remote_docker,
        cluster as cluster,
        ray as ray,
        parallelism as parallelism,
        cluster_health as cluster_health,
        launch_script as launch_script,
        health as health,
        reconciliation as reconciliation,
        locking as locking,
        events as events,
    )
else:
    from spark_pulse.tools import (
        system as system,
        cache as cache,
        recipes as recipes,
        recipe_import as recipe_import,
        deployments as deployments,
        benchmarking as benchmarking,
        custom_files as custom_files,
        custom_recipes as custom_recipes,
        mods as mods,
        git_update as git_update,
        docker as docker,
        network as network,
        discovery as discovery,
        cluster_models as cluster_models,
        ssh as ssh,
        remote_docker as remote_docker,
        cluster as cluster,
        ray as ray,
        parallelism as parallelism,
        cluster_health as cluster_health,
        launch_script as launch_script,
        health as health,
        reconciliation as reconciliation,
        locking as locking,
        events as events,
    )


def is_simulation() -> bool:
    """Return whether simulation mode is active."""
    return _sim_mode
