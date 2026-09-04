"""Tool module factory — loads real or mock tools based on SIMULATION_MODE.

Set SIMULATION_MODE=1 environment variable to use mock tools.
Default: 0 (real tools — production mode).
"""

from __future__ import annotations

import os

# Real-only modules, imported here so ``tools.<name>`` resolves in both modes.
# They hold pure data, parsing and filesystem primitives with nothing to
# simulate: ``labels`` and ``atomic_json``; ``hub_cache``, which is also the
# file copied to a worker node and run by its own python, so it must stay
# importable as itself; ``deployment_records``, which picks a different *path*
# under SIMULATION_MODE but runs the same code; ``custom_files`` and
# ``custom_recipes``, which read and write the operator's own config directory
# in either mode (a customization saved through the API must read back the same
# in simulation as in production, so there is one store and not two); and
# ``recipe_schema``/``recipe_sources``, which both the real and the mock
# ``recipes`` import (they are not listed below for exactly that reason — the
# switch must not see them). Everything else must exist in both packages.
from spark_pulse.tools import atomic_json as atomic_json
from spark_pulse.tools import custom_files as custom_files
from spark_pulse.tools import custom_recipes as custom_recipes
from spark_pulse.tools import deployment_records as deployment_records
from spark_pulse.tools import hub_cache as hub_cache
from spark_pulse.tools import labels as labels
from spark_pulse.tools import recipe_schema as recipe_schema
from spark_pulse.tools import recipe_sources as recipe_sources

_sim_mode = os.environ.get("SIMULATION_MODE", "0") == "1"

if _sim_mode:
    from spark_pulse.mock import (
        system as system,
        cache as cache,
        recipes as recipes,
        recipe_import as recipe_import,
        benchmarking as benchmarking,
        mods as mods,
        docker as docker,
        registry as registry,
        discovery as discovery,
        ssh as ssh,
        node_service as node_service,
        node_registry as node_registry,
        parallelism as parallelism,
        launch_script as launch_script,
        health as health,
        reconciliation as reconciliation,
        locking as locking,
        events as events,
        native_runtime as native_runtime,
        deploy_dispatch as deploy_dispatch,
        models as models,
        images as images,
        preflight as preflight,
    )
else:
    from spark_pulse.tools import (
        system as system,
        cache as cache,
        recipes as recipes,
        recipe_import as recipe_import,
        benchmarking as benchmarking,
        mods as mods,
        docker as docker,
        registry as registry,
        discovery as discovery,
        ssh as ssh,
        node_service as node_service,
        node_registry as node_registry,
        parallelism as parallelism,
        launch_script as launch_script,
        health as health,
        reconciliation as reconciliation,
        locking as locking,
        events as events,
        native_runtime as native_runtime,
        deploy_dispatch as deploy_dispatch,
        models as models,
        images as images,
        preflight as preflight,
    )


def is_simulation() -> bool:
    """Return whether simulation mode is active."""
    return _sim_mode
