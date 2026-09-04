"""Mock native runtime — the real implementation over the mock container service.

Nothing here needs simulating: :mod:`spark_pulse.tools.native_runtime` reaches
every side effect through ``spark_pulse.tools`` (container service, recipes,
model catalogue, deployment records), so simulation mode already swaps them
underneath it. The readiness probe short-circuits to success under
``SIMULATION_MODE``. This module exists so the real/mock module pairing stays
complete and ``tools.native_runtime`` resolves in both modes.
"""

from spark_pulse.tools.native_runtime import (
    CONTAINER_PREFIX as CONTAINER_PREFIX,
    MODS_DIR as MODS_DIR,
    RUNTIME_NAME as RUNTIME_NAME,
    SCRIPT_PATH as SCRIPT_PATH,
    ContainerSpec as ContainerSpec,
    DeployPlan as DeployPlan,
    NativeRuntimeError as NativeRuntimeError,
    RankPlan as RankPlan,
    allocate_port as allocate_port,
    cancel_pull as cancel_pull,
    container_name_for as container_name_for,
    create_deployment as create_deployment,
    delete_deployment as delete_deployment,
    get_deployment as get_deployment,
    get_logs as get_logs,
    identity_labels as identity_labels,
    is_native as is_native,
    list_deployments as list_deployments,
    persist_planned_record as persist_planned_record,
    plan as plan,
    probe_ready as probe_ready,
    pull_is_active as pull_is_active,
    publish_event as publish_event,
    rank_container_name as rank_container_name,
    rank_entries as rank_entries,
    rank_services as rank_services,
    register_event_loop as register_event_loop,
    start as start,
    status as status,
    stop_deployment as stop_deployment,
    sweep_orphans as sweep_orphans,
)
