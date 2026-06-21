"""Mock mods tools — mod listing, inspection, and cluster deployment simulation.

Returns deterministic results without accessing the filesystem.
Mirrors the real mods.py API exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from spark_pulse.tools.launch_script import ValidationResult


# Simulated mod directory structure
_MODS: list[dict[str, Any]] = [
    {
        "id": "fix-qwen3.5-autoround",
        "description": "Fixes AutoRound quantization for Qwen3.5 models",
        "files": [
            {"name": "run.sh", "kind": "script"},
            {"name": "fix-quant.patch", "kind": "patch"},
            {"name": "config.yaml", "kind": "yaml"},
        ],
        "has_patches": True,
        "script": "#!/bin/bash\n# Apply quantization fix\necho 'Applying fix...'",
    },
    {
        "id": "tuning-benchmark",
        "description": "Adds benchmarking hooks to the training loop",
        "files": [
            {"name": "run.sh", "kind": "script"},
            {"name": "hooks.py", "kind": "python"},
            {"name": "metrics.json", "kind": "json"},
        ],
        "has_patches": False,
        "script": "#!/bin/bash\n# Install benchmarking hooks\necho 'Installing hooks...'",
    },
    {
        "id": "nccl-optimization",
        "description": "Optimizes NCCL settings for DGX Spark",
        "files": [
            {"name": "run.sh", "kind": "script"},
            {"name": "nccl.conf.jinja", "kind": "template"},
        ],
        "has_patches": False,
        "script": "#!/bin/bash\n# Configure NCCL\necho 'Setting NCCL optimizations...'",
    },
]


@dataclass
class _MockModDeployment:
    """Internal tracking for mock mod deployment."""

    mod_name: str
    mod_path: Path
    target: Literal["head", "workers", "all"]
    completed_nodes: list[str] = field(default_factory=list)
    failed_nodes: list[str] = field(default_factory=list)


def list_mods() -> list[dict[str, Any]]:
    """List all available mods (simulated)."""
    return list(_MODS)


def get_mod(mod_id: str) -> dict[str, Any] | None:
    """Get detailed info for a specific mod, including its script.

    Returns None if the mod doesn't exist.
    """
    for mod in _MODS:
        if mod["id"] == mod_id:
            return dict(mod)
    return None


def validate_mod_content(mod_path: Path) -> ValidationResult:
    """Mock mod content validation.

    Simulates security scanning with scenario-driven results.
    """
    mod_name = mod_path.name

    # Simulate dangerous mod detection
    if "dangerous" in mod_name.lower():
        return ValidationResult.fail(
            errors=[
                "Dangerous pattern detected in run.sh: rm\\s+-rf\\s+/"
            ],
            warnings=["run.sh uses sudo"],
        )

    # Simulate size limit exceeded
    if "oversized" in mod_name.lower():
        return ValidationResult.fail(
            errors=[
                "Mod exceeds maximum size 52428800 bytes"
            ],
        )

    # Simulate zip bomb
    if "zipbomb" in mod_name.lower():
        return ValidationResult.fail(
            errors=[
                "Possible zip bomb: compression ratio 15.3x"
            ],
        )

    # Valid mod with optional warnings
    warnings: list[str] = []
    if "network" in mod_name.lower():
        warnings.append("run.sh uses network access (curl/wget)")

    return ValidationResult.ok(warnings=warnings if warnings else None)


class MockModOrchestrator:
    """Mock mod orchestrator for cluster-wide deployment simulation.

    Scenario-driven simulation:
    - "success": all nodes succeed
    - "partial_failure": some nodes fail
    - "head_only": only head node is targeted
    """

    def __init__(self, scenario: str = "success"):
        self._scenario = scenario
        self._deployments: list[_MockModDeployment] = []

    def apply_mod_cluster(
        self,
        mod_deployment: Any,
        cluster_state: Any,
    ) -> Any:
        """Apply mod to cluster nodes based on target.

        Returns a ModDeployment-like object with completed/failed tracking.
        """
        completed: list[str] = []
        failed: list[str] = []

        # Determine target nodes
        target_nodes: list[Any] = []
        if mod_deployment.target in ("head", "all"):
            target_nodes.append(cluster_state.head)
        if mod_deployment.target in ("workers", "all"):
            target_nodes.extend(cluster_state.workers)

        for node in target_nodes:
            if self._scenario == "success":
                completed.append(node.ip)
            elif self._scenario == "partial_failure":
                # First half succeeds, second half fails
                idx = target_nodes.index(node)
                if idx < len(target_nodes) // 2:
                    completed.append(node.ip)
                else:
                    failed.append(node.ip)
            elif self._scenario == "all_fail":
                failed.append(node.ip)
            else:
                completed.append(node.ip)

        deployment = _MockModDeployment(
            mod_name=mod_deployment.mod_name,
            mod_path=mod_deployment.mod_path,
            target=mod_deployment.target,
            completed_nodes=completed,
            failed_nodes=failed,
        )
        self._deployments.append(deployment)
        return deployment

    def rollback_mod(
        self,
        mod_deployment: Any,
        cluster_state: Any,
    ) -> list[str]:
        """Rollback mod on completed nodes."""
        rolled_back: list[str] = []

        target_nodes: list[Any] = []
        if mod_deployment.target in ("head", "all"):
            target_nodes.append(cluster_state.head)
        if mod_deployment.target in ("workers", "all"):
            target_nodes.extend(cluster_state.workers)

        for node in target_nodes:
            if node.ip in mod_deployment.completed_nodes:
                rolled_back.append(node.ip)

        return rolled_back

    def validate_mod(
        self,
        mod_path: Path,
    ) -> ValidationResult:
        """Validate mod content (delegates to validate_mod_content)."""
        return validate_mod_content(mod_path)

    @property
    def deployments(self) -> list[_MockModDeployment]:
        """Return all recorded deployments."""
        return list(self._deployments)
