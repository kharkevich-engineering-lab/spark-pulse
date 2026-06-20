"""Mock mods tools — mod listing and inspection simulation.

Returns deterministic results without accessing the filesystem.
Mirrors the real mods.py API exactly.
"""

from __future__ import annotations

from typing import Any

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
