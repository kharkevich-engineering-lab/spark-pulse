"""The fabric planner is pure, so the simulated twin is the real one.

Nothing in :mod:`spark_pulse.tools.fabric_plan` touches a machine; the
simulated control plane feeds it simulated facts and gets real plans.
"""

from __future__ import annotations

from spark_pulse.tools.fabric_plan import (  # noqa: F401 — re-exported
    FABRIC_MTU as FABRIC_MTU,
    NETPLAN_PATH as NETPLAN_PATH,
    STATUS_CONFIGURED as STATUS_CONFIGURED,
    STATUS_PROPOSED as STATUS_PROPOSED,
    STATUS_REFUSED as STATUS_REFUSED,
    STATUS_UNKNOWN as STATUS_UNKNOWN,
    Assignment as Assignment,
    FabricPlan as FabricPlan,
    NodeFabric as NodeFabric,
    NodePlan as NodePlan,
    plan_fabric as plan_fabric,
    render_netplan as render_netplan,
)
