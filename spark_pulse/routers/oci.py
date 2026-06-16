"""OCI registry REST API router.

Provides endpoints for registry management, collection browsing,
installation, update checking, and auto-update configuration.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from spark_pulse.tools import is_simulation
from spark_pulse.tools.oci_registry import (
    add_registry,
    apply_updates,
    check_updates,
    get_oci_meta,
    install_collection,
    install_oci_recipe,
    list_collection_recipes,
    list_collections,
    list_oci_recipes,
    list_registries,
    remove_registry,
    run_auto_update,
    save_auto_update_log,
    test_registry_connection,
    update_oci_recipe,
    update_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oci", tags=["oci"])


# ── Registries ───────────────────────────────────────────────────────────────


@router.get("/registries")
def get_registries():
    """List all configured registries with connectivity status."""
    if is_simulation():
        return _mock_reg_state
    return list_registries()


@router.post("/registries")
def create_registry(body: dict):
    """Add a new registry."""
    if is_simulation():
        return _mock_add_registry(body)
    name = body.get("name")
    url = body.get("url")
    if not name or not url:
        raise HTTPException(status_code=400, detail="name and url are required")
    try:
        return add_registry(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/registries/{name}")
def update_registry_endpoint(name: str, body: dict):
    """Update an existing registry."""
    if is_simulation():
        return _mock_update_registry(name, body)
    result = update_registry(name, body)
    if not result:
        raise HTTPException(status_code=404, detail=f"Registry '{name}' not found")
    return result


@router.delete("/registries/{name}")
def delete_registry(name: str):
    """Remove a registry."""
    if is_simulation():
        return _mock_delete_registry(name)
    if not remove_registry(name):
        raise HTTPException(status_code=404, detail=f"Registry '{name}' not found")
    return {"deleted": True}


@router.post("/registries/{name}/test-connection")
def test_connection(name: str):
    """Test connectivity to a registry."""
    if is_simulation():
        return {"ok": True, "registry": name}
    ok = test_registry_connection(name)
    return {"ok": ok, "registry": name}


# ── Collections (Browse) ─────────────────────────────────────────────────────


@router.get("/collections")
def get_collections(
    registry: str = Query(None, description="Registry name to filter by"),
    version: str = Query(None, description="Version tag to filter by"),
):
    """List available recipe collections from one or more registries."""
    if is_simulation():
        return _mock_collections(registry=registry, version=version)
    try:
        collections = list_collections(registry_name=registry, version=version)
        return [
            {
                "name": c.name,
                "version": c.version,
                "description": c.description,
                "vendor": c.vendor,
                "license": c.license,
                "recipe_count": c.recipe_count,
                "digest": c.digest,
                "registry": c.registry,
            }
            for c in collections
        ]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to list collections: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/collections/{name}/recipes")
def get_collection_recipes(
    name: str,
    version: str = Query(None, description="Version tag to filter by"),
    registry: str = Query(None, description="Registry name to filter by"),
):
    """List individual recipes in a collection."""
    if is_simulation():
        return _mock_collection_recipes(name)
    try:
        recipes = list_collection_recipes(
            collection_name=name, version=version, registry_name=registry
        )
        return [
            {
                "name": r.name,
                "description": r.description or "",
                "model": r.model or "",
                "container": r.container or "",
                "recipe_version": r.recipe_version or "",
            }
            for r in recipes
        ]
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to list collection recipes: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Install ──────────────────────────────────────────────────────────────────


@router.post("/install")
def post_install(body: dict):
    """Install a recipe collection from an OCI registry."""
    if is_simulation():
        return _mock_install_collection(body)
    name = body.get("name")
    version = body.get("version")
    registry = body.get("registry")

    if not name or not version:
        raise HTTPException(
            status_code=400,
            detail="name and version are required",
        )

    try:
        installed = install_collection(
            name=name,
            version=version,
            registry_name=registry,
        )
        return {"installed": installed}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Install failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/recipes/install")
def install_oci_recipe_endpoint(body: dict):
    """Install a single recipe from a collection."""
    if is_simulation():
        return _mock_install_recipe(body)
    collection = body.get("collection")
    recipe = body.get("recipe")
    version = body.get("version")
    registry = body.get("registry")
    overwrite = body.get("overwrite", False)

    if not collection or not recipe:
        raise HTTPException(
            status_code=400, detail="collection and recipe are required"
        )

    try:
        result = install_oci_recipe(
            collection_name=collection,
            recipe_name=recipe,
            version=version or "",
            registry_name=registry,
            overwrite=overwrite,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Recipe install failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/recipes/update/{recipe_name}")
def update_oci_recipe_endpoint(
    recipe_name: str,
    body: dict = {},
):
    """Update an existing OCI-installed recipe."""
    if is_simulation():
        return _mock_update_recipe(recipe_name, body)
    collection = body.get("collection")
    version = body.get("version")
    registry = body.get("registry")

    if not collection:
        raise HTTPException(status_code=400, detail="collection is required")

    try:
        result = update_oci_recipe(
            recipe_name=recipe_name,
            collection_name=collection,
            version=version,
            registry_name=registry,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Recipe update failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Update Check ─────────────────────────────────────────────────────────────


@router.get("/check")
def get_update_check(
    collection: str = Query(None, description="Filter by collection name"),
    registry: str = Query(None, description="Filter by registry name"),
):
    """Check for available updates for installed OCI recipes."""
    if is_simulation():
        return _mock_updates()
    try:
        updates = check_updates(collection=collection, registry=registry)
        return [
            {
                "collection": u.collection,
                "current_version": u.current_version,
                "latest_version": u.latest_version,
                "current_digest": u.current_digest,
                "latest_digest": u.latest_digest,
                "local_changes": u.local_changes,
                "added_recipes": u.added_recipes,
                "modified_recipes": u.modified_recipes,
            }
            for u in updates
        ]
    except Exception as exc:
        logger.error("Update check failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/update")
def post_update(body: dict):
    """Apply pending updates."""
    updates = body.get("updates", [])
    overwrite = body.get("overwrite_local", False)

    if not updates:
        raise HTTPException(status_code=400, detail="updates array is required")

    try:
        results = apply_updates(updates, overwrite_local=overwrite)
        return results
    except Exception as exc:
        logger.error("Update application failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Metadata ─────────────────────────────────────────────────────────────────


@router.get("/recipes/meta")
def get_oci_recipes_meta():
    """List all installed OCI recipes with their metadata."""
    if is_simulation():
        return _mock_oci_meta()
    metas = list_oci_recipes()
    return [
        {
            "name": m.name,
            "source": m.source,
            "collection": m.collection,
            "version": m.version,
            "digest": m.digest,
            "installed_at": m.installed_at,
            "updated_at": m.updated_at,
            "local_changes": m.local_changes,
        }
        for m in metas
    ]


@router.get("/recipes/meta/{recipe_name}")
def get_oci_recipe_meta(recipe_name: str):
    """Get metadata for a specific OCI-installed recipe."""
    meta = get_oci_meta(recipe_name)
    if not meta:
        raise HTTPException(
            status_code=404,
            detail=f"No OCI metadata found for '{recipe_name}'",
        )
    return {
        "name": meta.name,
        "source": meta.source,
        "collection": meta.collection,
        "version": meta.version,
        "digest": meta.digest,
        "installed_at": meta.installed_at,
        "updated_at": meta.updated_at,
        "local_changes": meta.local_changes,
    }


# ── Auto-Update ──────────────────────────────────────────────────────────────


@router.get("/auto-update/settings")
def get_auto_update_settings():
    """Get auto-update configuration and status."""
    from spark_pulse.config import config

    enabled = getattr(config, "oci_auto_update_enabled", False)
    schedule = getattr(config, "oci_auto_update_schedule", "0 2 * * *")
    overwrite = getattr(config, "oci_auto_update_overwrite_local", False)

    return {
        "enabled": enabled,
        "schedule": schedule,
        "overwrite_local": overwrite,
    }


@router.put("/auto-update/settings")
def update_auto_update_settings(body: dict):
    """Update auto-update configuration."""
    from spark_pulse.config import config

    # Update config values
    if "enabled" in body:
        config.oci_auto_update_enabled = bool(body["enabled"])
    if "schedule" in body:
        config.oci_auto_update_schedule = body["schedule"]
    if "overwrite_local" in body:
        config.oci_auto_update_overwrite_local = bool(body["overwrite_local"])

    return {
        "enabled": config.oci_auto_update_enabled,
        "schedule": config.oci_auto_update_schedule,
        "overwrite_local": config.oci_auto_update_overwrite_local,
    }


@router.post("/auto-update/run")
def run_auto_update_endpoint():
    """Manually trigger an auto-update run."""
    try:
        result = run_auto_update()
        save_auto_update_log()
        return result
    except Exception as exc:
        logger.error("Auto-update run failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Mock data (for development/testing) ──────────────────────────────────────


def _mock_registries():
    return [
        {
            "name": "spark-official",
            "url": "ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
            "enabled": True,
            "default": True,
            "auth_type": "token",
            "connected": True,
        },
        {
            "name": "my-registry",
            "url": "registry.example.com/my-org/recipes",
            "enabled": False,
            "default": False,
            "auth_type": "none",
            "connected": False,
            "error": "Registry not configured",
        },
    ]


# Mutable mock state for simulation CRUD
_mock_reg_state: list[dict] = [
    {
        "name": "ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
        "url": "ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
        "enabled": True,
        "default": True,
        "auth_type": "token",
        "connected": True,
    },
    {
        "name": "my-registry",
        "url": "registry.example.com/my-org/recipes",
        "enabled": False,
        "default": False,
        "auth_type": "none",
        "connected": False,
        "error": "Registry not configured",
    },
]


def _mock_add_registry(body: dict) -> dict:
    """Add a registry to mock state."""
    name = body.get("name", "")
    url = body.get("url", "")
    # Deduplicate by name
    _mock_reg_state[:] = [r for r in _mock_reg_state if r["name"] != name]
    reg = {
        "name": name,
        "url": url,
        "enabled": body.get("enabled", True),
        "default": body.get("default", False),
        "auth_type": body.get("auth_type", "none"),
        "connected": False,
    }
    _mock_reg_state.append(reg)
    return reg


def _mock_update_registry(name: str, body: dict) -> dict:
    """Update a registry in mock state."""
    for i, r in enumerate(_mock_reg_state):
        if r["name"] == name:
            for k, v in body.items():
                _mock_reg_state[i][k] = v
            return _mock_reg_state[i]
    raise HTTPException(status_code=404, detail=f"Registry '{name}' not found")


def _mock_delete_registry(name: str) -> dict:
    """Delete a registry from mock state."""
    before = len(_mock_reg_state)
    _mock_reg_state[:] = [r for r in _mock_reg_state if r["name"] != name]
    if len(_mock_reg_state) < before:
        return {"deleted": True}
    raise HTTPException(status_code=404, detail=f"Registry '{name}' not found")


def _mock_collections(registry: str | None = None, version: str | None = None):
    collections = [
        {
            "name": "spark-recipes",
            "version": "1.0.0",
            "description": "Spark Pulse recipe collection",
            "vendor": "Kharkevich Engineering Lab",
            "license": "MIT",
            "recipe_count": 5,
            "digest": "sha256:abc123def456",
            "registry": "ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
        },
        {
            "name": "community-recipes",
            "version": "0.3.0",
            "description": "Community-contributed recipes",
            "vendor": "Community",
            "license": "Apache-2.0",
            "recipe_count": 3,
            "digest": "sha256:789ghi012jkl",
            "registry": "ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
        },
    ]
    if registry:
        collections = [c for c in collections if c["registry"] == registry]
    if version:
        collections = [c for c in collections if c["version"] == version]
    return collections


def _mock_install_collection(body: dict) -> dict:
    """Mock collection installation."""
    name = body.get("name", "")
    version = body.get("version", "")
    # Check if collection exists in mock data
    collections = _mock_collections()
    matching = [c for c in collections if c["name"] == name and c["version"] == version]
    if not matching:
        raise HTTPException(status_code=404, detail=f"Collection '{name}:{version}' not found")
    # Return mock installed recipes
    recipes_map = {
        "spark-recipes": [f"spark-vllm-{size}b.yaml" for size in ["7b", "13b", "20b", "40b", "70b"]],
        "community-recipes": ["community-llama-3-8b.yaml"],
    }
    installed = recipes_map.get(name, [f"{name}.yaml"])
    return {"installed": installed}


def _mock_collection_recipes(name: str) -> list[dict]:
    """Mock recipe listing for a collection."""
    recipes_map = {
        "spark-recipes": [
            {
                "name": "spark-vllm-7b",
                "description": "Llama 3.1 8B inference with vLLM",
                "model": "meta-llama/Llama-3.1-8B-Instruct",
                "container": "vllm-node",
                "recipe_version": "1.0.0",
            },
            {
                "name": "spark-vllm-13b",
                "description": "Llama 3.1 70B inference with vLLM",
                "model": "meta-llama/Llama-3.1-70B-Instruct",
                "container": "vllm-node",
                "recipe_version": "1.0.0",
            },
            {
                "name": "spark-vllm-20b",
                "description": "Mistral 22B inference with vLLM",
                "model": "mistralai/Mistral-22B-Instruct-v0.1",
                "container": "vllm-node",
                "recipe_version": "1.0.0",
            },
            {
                "name": "spark-vllm-40b",
                "description": "Mixtral 8x7B inference with vLLM",
                "model": "mistralai/Mixtral-8x7B-Instruct-v0.1",
                "container": "vllm-node",
                "recipe_version": "1.0.0",
            },
            {
                "name": "spark-vllm-70b",
                "description": "Llama 3.1 70B optimized inference",
                "model": "meta-llama/Llama-3.1-70B-Instruct",
                "container": "vllm-node",
                "recipe_version": "1.0.0",
            },
        ],
        "community-recipes": [
            {
                "name": "community-llama-3-8b",
                "description": "Community-tuned Llama 3 8B",
                "model": "meta-llama/Llama-3-8B",
                "container": "vllm-node",
                "recipe_version": "0.3.0",
            },
            {
                "name": "community-mixtral-8x7b",
                "description": "Community-tuned Mixtral 8x7B",
                "model": "mistralai/Mixtral-8x7B-Instruct-v0.1",
                "container": "vllm-node",
                "recipe_version": "0.3.0",
            },
            {
                "name": "community-qwen-72b",
                "description": "Qwen 2.5 72B inference",
                "model": "Qwen/Qwen2.5-72B-Instruct",
                "container": "vllm-node",
                "recipe_version": "0.3.0",
            },
        ],
    }
    return recipes_map.get(name, [])


# Mock state for installed individual recipes
_mock_installed_recipes: set[str] = set()


def _mock_install_recipe(body: dict) -> dict:
    """Mock install a single recipe."""
    recipe = body.get("recipe", "")
    collection = body.get("collection", "")
    key = f"{collection}/{recipe}"
    if key in _mock_installed_recipes:
        return {"success": True, "recipe": recipe, "action": "up_to_date"}
    _mock_installed_recipes.add(key)
    return {"success": True, "recipe": recipe, "action": "installed"}


def _mock_update_recipe(recipe_name: str, body: dict) -> dict:
    """Mock update a single recipe."""
    collection = body.get("collection", "")
    key = f"{collection}/{recipe_name}"
    if key not in _mock_installed_recipes:
        raise HTTPException(
            status_code=404, detail=f"Recipe '{recipe_name}' is not installed"
        )
    return {"success": True, "recipe": recipe_name, "action": "updated"}


def _mock_updates():
    return [
        {
            "collection": "spark-recipes",
            "current_version": "1.0.0",
            "latest_version": "1.1.0",
            "current_digest": "sha256:abc123def456",
            "latest_digest": "sha256:new789xyz",
            "local_changes": False,
            "added_recipes": ["spark-vllm-70b.yaml"],
            "modified_recipes": ["spark-vllm-7b.yaml"],
        },
    ]


def _mock_oci_meta():
    return [
        {
            "name": "spark-vllm-7b.yaml",
            "source": "spark-official",
            "collection": "spark-recipes",
            "version": "1.0.0",
            "digest": "sha256:abc123",
            "installed_at": "2026-06-15T02:00:00Z",
            "updated_at": "2026-06-15T02:00:00Z",
            "local_changes": False,
        },
        {
            "name": "spark-vllm-13b.yaml",
            "source": "spark-official",
            "collection": "spark-recipes",
            "version": "1.0.0",
            "digest": "sha256:def456",
            "installed_at": "2026-06-15T02:00:00Z",
            "updated_at": "2026-06-15T02:00:00Z",
            "local_changes": False,
        },
    ]
