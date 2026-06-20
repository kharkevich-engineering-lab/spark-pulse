"""Host network discovery REST API router.

Provides endpoints for discovering host network configuration,
detecting interfaces/InfiniBand, and auto-generating NCCL defaults.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from spark_pulse.config import config
from spark_pulse.tools import is_simulation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


def _get_discovery_module():
    """Get the discovery module (real or mock)."""
    if is_simulation():
        from spark_pulse.mock import discovery as discovery_module
    else:
        from spark_pulse.tools import discovery as discovery_module
    return discovery_module


def _serialize_discovery(result: Any) -> dict[str, Any]:
    """Serialize a DiscoveryResult to a JSON-compatible dict."""
    return {
        "local_ip": result.local_ip,
        "ethernet_if": result.ethernet_if,
        "infiniband_present": result.infiniband_present,
        "infiniband_devices": [
            {
                "hca": d.hca,
                "ports": d.ports,
                "net_devices": d.net_devices,
                "state": d.state,
            }
            for d in result.infiniband_devices
        ],
        "interfaces": [
            {
                "name": i.name,
                "ip": i.ip,
                "mtu": i.mtu,
                "is_up": i.is_up,
                "type": i.type,
            }
            for i in result.interfaces
        ],
        "nccl_defaults": (
            {
                "socket_ifname": result.nccl_defaults.socket_ifname,
                "ib_hca": result.nccl_defaults.ib_hca,
                "ib_disable": result.nccl_defaults.ib_disable,
            }
            if result.nccl_defaults
            else None
        ),
        "validation_errors": result.validation_errors,
    }


def _serialize_validation(result: Any) -> dict[str, Any]:
    """Serialize a ValidationResult to a JSON-compatible dict."""
    return {
        "healthy": result.healthy,
        "warnings": result.warnings,
        "errors": result.errors,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("")
def run_discovery():
    """Run network discovery and return detected + recommended changes.

    Returns:
    {
        "detected": {...DiscoveryResult},
        "validation": {...ValidationResult}
    }
    """
    try:
        discovery_module = _get_discovery_module()
        detected = discovery_module.run_discovery()
        validation = discovery_module.validate_network()

        return {
            "detected": _serialize_discovery(detected),
            "validation": _serialize_validation(validation),
        }
    except Exception as e:
        logger.exception("Discovery failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
def get_discovered():
    """Return last discovered config (from NCCL defaults in config).

    Returns the NCCL defaults that were previously auto-detected and
    optionally persisted into config.yaml / settings.json.
    """
    try:
        # Read NCCL config from current config
        nccl_config = config._data.get("nccl", {})
        return {
            "nccl": {
                "debug": nccl_config.get("debug"),
                "socket_ifname": nccl_config.get("socket_ifname"),
                "ib_hca": nccl_config.get("ib_hca"),
            },
            "discovery_available": not is_simulation(),
        }
    except Exception as e:
        logger.exception("Failed to get discovered config")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/apply-nccl")
def apply_nccl_defaults(body: dict[str, Any]):
    """Apply detected NCCL defaults to config.

    Request body:
        socket_ifname: str — Interface name for NCCL sockets
        ib_hca: str | None — InfiniBand HCA name (optional)
        ib_disable: bool — Whether to disable InfiniBand for NCCL

    Persists values into settings.json via config.
    """
    try:
        socket_ifname = body.get("socket_ifname")
        ib_hca = body.get("ib_hca")
        ib_disable = body.get("ib_disable", False)

        if not socket_ifname:
            raise HTTPException(status_code=400, detail="socket_ifname is required")

        # Update nccl config
        if "nccl" not in config._data:
            config._data["nccl"] = {}

        config._data["nccl"]["socket_ifname"] = socket_ifname
        config._data["nccl"]["ib_hca"] = ib_hca
        config._data["nccl"]["ib_disable"] = ib_disable

        # Save to settings.json
        from spark_pulse.config import _save_user_settings

        _save_user_settings(config._data)

        return {
            "success": True,
            "applied": {
                "socket_ifname": socket_ifname,
                "ib_hca": ib_hca,
                "ib_disable": ib_disable,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to apply NCCL defaults")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/validation")
def get_validation():
    """Run and return network health validation.

    Returns:
    {
        "healthy": bool,
        "warnings": [...],
        "errors": [...]
    }
    """
    try:
        discovery_module = _get_discovery_module()
        validation = discovery_module.validate_network()
        return _serialize_validation(validation)
    except Exception as e:
        logger.exception("Validation failed")
        raise HTTPException(status_code=500, detail=str(e))
