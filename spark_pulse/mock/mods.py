"""Mock mods tools — listing, inspection and validation.

Returns deterministic results without accessing the filesystem. Mirrors the
real mods.py API exactly, which no longer includes applying a mod: a recipe
names its mods and the native runtime copies them into each rank's container
at deploy time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
            errors=["Dangerous pattern detected in run.sh: rm\\s+-rf\\s+/"],
            warnings=["run.sh uses sudo"],
        )

    # Simulate size limit exceeded
    if "oversized" in mod_name.lower():
        return ValidationResult.fail(
            errors=["Mod exceeds maximum size 52428800 bytes"],
        )

    # Simulate zip bomb
    if "zipbomb" in mod_name.lower():
        return ValidationResult.fail(
            errors=["Possible zip bomb: compression ratio 15.3x"],
        )

    # Valid mod with optional warnings
    warnings: list[str] = []
    if "network" in mod_name.lower():
        warnings.append("run.sh uses network access (curl/wget)")

    return ValidationResult.ok(warnings=warnings if warnings else None)
