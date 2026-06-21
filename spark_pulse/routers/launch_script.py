"""Launch script API endpoints.

Provides endpoints for resolving, analyzing, and validating launch scripts
for cluster deployments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from spark_pulse.tools import launch_script

router = APIRouter(prefix="/api/launch-script", tags=["launch-script"])


@router.post("/resolve")
def resolve_launch_script(req: dict[str, Any]) -> dict[str, Any]:
    """Resolve and validate a launch script path.

    Returns the resolved path and basic info.
    """
    path = req.get("path", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")

    try:
        manager = launch_script.LaunchScriptManager()
        resolved = manager.resolve(path)
        return {
            "path": str(resolved),
            "exists": resolved.exists(),
            "is_file": resolved.is_file(),
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze")
def analyze_launch_script(req: dict[str, Any]) -> dict[str, Any]:
    """Analyze launch script: extract parallelism, detect backend, validate.

    Returns LaunchScriptInfo as dict for frontend preview.
    """
    path = req.get("path", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")

    try:
        script_path = Path(path)
        info = launch_script.analyze_launch_script(script_path)

        result: dict[str, Any] = {
            "path": str(info.path),
            "command_line": info.command_line,
            "parallelism": info.parallelism,
            "backend": info.backend,
            "has_model_flag": info.has_model_flag,
            "is_valid": info.is_valid,
        }

        if info.validation:
            result["validation"] = {
                "healthy": info.validation.healthy,
                "warnings": info.validation.warnings,
                "errors": info.validation.errors,
            }

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/validate")
def validate_launch_script(req: dict[str, Any]) -> dict[str, Any]:
    """Validate a launch script before deployment.

    Returns ValidationResult as dict.
    """
    path = req.get("path", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")

    try:
        script_path = Path(path)
        validation = launch_script.validate_launch_script(script_path)

        return {
            "healthy": validation.healthy,
            "warnings": validation.warnings,
            "errors": validation.errors,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/patch")
def patch_launch_script(req: dict[str, Any]) -> dict[str, Any]:
    """Create patched script bundle for cluster deployment.

    Returns bundle info with per-node script paths.
    """
    path = req.get("path", "")
    total_nodes = req.get("total_nodes", 1)
    master_addr = req.get("master_addr", "127.0.0.1")
    master_port = req.get("master_port", 29500)

    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    if total_nodes < 1:
        raise HTTPException(status_code=400, detail="total_nodes must be >= 1")

    try:
        script_path = Path(path)
        manager = launch_script.LaunchScriptManager()
        bundle = manager.create_patched_bundle(
            script_path=script_path,
            total_nodes=total_nodes,
            master_addr=master_addr,
            master_port=master_port,
        )

        result = {
            "original_script": str(bundle.original_script),
            "total_nodes": bundle.total_nodes,
            "master_addr": bundle.master_addr,
            "master_port": bundle.master_port,
            "scripts": {
                str(rank): str(script)
                for rank, script in bundle.scripts.items()
            },
        }

        # Cleanup the bundle after reading paths
        manager.cleanup(bundle)

        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
