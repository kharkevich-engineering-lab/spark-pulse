"""Mock deployment dispatcher — the real router over the mock runtimes.

The dispatcher only decides *which* module handles a call and reaches both of
them through ``spark_pulse.tools``, so simulation mode already swaps the
implementations underneath it. Re-exported here so the real/mock pairing stays
complete and ``tools.deploy_dispatch`` resolves in both modes.
"""

from spark_pulse.tools.deploy_dispatch import (
    active_runtime as active_runtime,
    create_deployment as create_deployment,
    delete_deployment as delete_deployment,
    get_deployment as get_deployment,
    get_logs as get_logs,
    list_deployments as list_deployments,
    plan_deployment as plan_deployment,
    stop_deployment as stop_deployment,
    uses_native as uses_native,
)
