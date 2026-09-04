"""Tool module factory — loads real or mock tools based on SIMULATION_MODE.

Set SIMULATION_MODE=1 environment variable to use mock tools.
Default: 0 (real tools — production mode).
"""

from __future__ import annotations

import os

# ``cluster_models``, ``labels``, ``atomic_json`` and ``hub_cache`` hold pure
# data, constants and filesystem primitives with no behaviour to simulate, so
# they are real-only and intentionally have no mock twin. ``hub_cache`` is also
# the file that gets copied to a worker node and run by its own python, so it
# must stay importable as itself in both modes. Everything else must exist in
# both packages.
from spark_pulse.tools import atomic_json as atomic_json
from spark_pulse.tools import hub_cache as hub_cache
from spark_pulse.tools import labels as labels

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
        docker as docker,
        registry as registry,
        network as network,
        discovery as discovery,
        ssh as ssh,
        node_service as node_service,
        node_registry as node_registry,
        cluster as cluster,
        ray as ray,
        parallelism as parallelism,
        cluster_health as cluster_health,
        launch_script as launch_script,
        health as health,
        reconciliation as reconciliation,
        locking as locking,
        events as events,
        native_runtime as native_runtime,
        deploy_dispatch as deploy_dispatch,
        models as models,
        images as images,
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
        docker as docker,
        registry as registry,
        network as network,
        discovery as discovery,
        cluster_models as cluster_models,
        ssh as ssh,
        node_service as node_service,
        node_registry as node_registry,
        cluster as cluster,
        ray as ray,
        parallelism as parallelism,
        cluster_health as cluster_health,
        launch_script as launch_script,
        health as health,
        reconciliation as reconciliation,
        locking as locking,
        events as events,
        native_runtime as native_runtime,
        deploy_dispatch as deploy_dispatch,
        models as models,
        images as images,
    )


def is_simulation() -> bool:
    """Return whether simulation mode is active."""
    return _sim_mode
