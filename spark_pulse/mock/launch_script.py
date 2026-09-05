"""Mock launch script tools — the real analysis, a simulated distributor.

Resolving, validating, analysing and patching a launch script is pure work on
a file the operator hands us: read it, regex it, write per-node copies into a
``TemporaryDirectory``. There is no subprocess, no SSH and no Docker in any of
it, so there is nothing to simulate — and simulating it anyway is worse than
not simulating it, because ``/api/launch-script/*`` would then answer one way
in simulation and another in production for the very same file. Those names
are re-exported from the real module, the way ``mock/system.py`` re-exports its
parsing helpers.

Only *distribution* reaches other machines, so only the distributor has a mock
twin here: it subclasses the real one and stubs the single leaf that copies a
script to a node, keeping the rank mapping and the per-node failure handling
that the real orchestration performs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spark_pulse.tools.launch_script import (
    DANGEROUS_PATTERNS as DANGEROUS_PATTERNS,
    LaunchScriptDistributor as _RealLaunchScriptDistributor,
    LaunchScriptInfo as LaunchScriptInfo,
    LaunchScriptManager as LaunchScriptManager,
    MAX_MOD_SIZE as MAX_MOD_SIZE,
    PatchedScriptBundle as PatchedScriptBundle,
    ValidationResult as ValidationResult,
    analyze_launch_script as analyze_launch_script,
    validate_launch_script as validate_launch_script,
    validate_mod_content as validate_mod_content,
)


class LaunchScriptDistributor(_RealLaunchScriptDistributor):
    """Records what would have been copied to each node instead of copying it.

    ``deploy_to_cluster`` is inherited, so simulation walks the same head/worker
    ranks and reports the same ``{rank: ok}`` map as production.
    """

    def __init__(self, ssh_client: Any = None, services: Any = None):
        # Deliberately not calling super().__init__: it would build the real
        # NodeServices, which talks to Docker.
        self._ssh = ssh_client
        self._services = services
        self.deployments: list[dict[str, Any]] = []

    def deploy_to_node(
        self,
        node: Any,
        script: Path,
        container_name: str,
    ) -> None:
        """Record a deployment rather than performing one."""
        self.deployments.append(
            {
                "node_ip": node.ip,
                "node_role": node.role,
                "container": container_name,
                "script": str(script),
            }
        )
