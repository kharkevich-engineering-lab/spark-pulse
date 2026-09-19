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
    clear_oci_cache,
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
    start_background_updater,
    stop_background_updater,
    test_registry_connection,
    uninstall_oci_recipe,
    update_oci_recipe,
    update_registry,
    _oras_list_tags,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/oci", tags=["oci"])


def _simulated():
    """The simulated registry, imported only when the switch asks for it.

    Every endpoint below picks its implementation — the real tool or this
    module's twin of the same name — and then serialises the result once.
    Imported inside the call because ``spark_pulse.mock`` pulls in every mock
    the package ships, and a production process has no use for any of them.
    """
    from spark_pulse.mock import oci_registry as simulated

    return simulated


# ── Registries ───────────────────────────────────────────────────────────────


#: Auth fields that are secrets. They leave this process only as a masked
#: marker: the browser has no reason to see a stored token or password, and
#: the edit dialog never pre-fills one. This is the same rule the settings
#: router applies to ``hf_token`` (``config.hf_token_masked``).
_SECRET_AUTH_KEYS = ("token", "password")
_MASK = "•" * 8


def _masked_secret(value: object) -> str:
    """The masked form of a stored secret, mirroring ``hf_token_masked``.

    The last four characters are kept only for a secret long enough that they
    identify it without weakening it (a token); a short password is hidden
    entirely.
    """
    text = str(value or "")
    if not text:
        return ""
    return _MASK + text[-4:] if len(text) >= 12 else _MASK


def _public_registry(registry: dict) -> dict:
    """A registry record safe to send to the browser: stored secrets masked.

    Applied at this boundary rather than in ``tools.oci_registry``, because the
    tool's own callers (``_oras_list_tags``, ``_auth_headers``) need the real
    credential to talk to the registry. Masking is idempotent, so a record
    that was already masked stays masked, and ``update_registry`` refuses to
    store a masked value, so nothing this returns can round-trip into the
    registries file.
    """
    public = dict(registry)
    auth = public.get("auth")
    if isinstance(auth, dict):
        masked = dict(auth)
        for key in _SECRET_AUTH_KEYS:
            if masked.get(key):
                masked[key] = _masked_secret(masked[key])
        public["auth"] = masked
    return public


@router.get("/registries")
def get_registries():
    """List all configured registries with connectivity status."""
    lister = _simulated().mock_list_registries if is_simulation() else list_registries
    return [_public_registry(r) for r in lister()]


@router.post("/registries")
def create_registry(body: dict):
    """Add a new registry."""
    name = body.get("name")
    url = body.get("url")
    if not name or not url:
        raise HTTPException(status_code=400, detail="name and url are required")
    adder = _simulated().mock_add_registry if is_simulation() else add_registry
    try:
        return _public_registry(adder(body))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/registries/{name}")
def update_registry_endpoint(name: str, body: dict):
    """Update an existing registry."""
    updater = _simulated().mock_update_registry if is_simulation() else update_registry
    result = updater(name, body)
    if not result:
        raise HTTPException(status_code=404, detail=f"Registry '{name}' not found")
    return _public_registry(result)


@router.delete("/registries/{name}")
def delete_registry(name: str):
    """Remove a registry."""
    remover = _simulated().mock_remove_registry if is_simulation() else remove_registry
    if not remover(name):
        raise HTTPException(status_code=404, detail=f"Registry '{name}' not found")
    return {"deleted": True}


@router.get("/registries/{name}/test-connection")
def test_connection(name: str):
    """Test connectivity to a registry."""
    tester = (
        _simulated().mock_test_registry_connection
        if is_simulation()
        else test_registry_connection
    )
    return {"ok": tester(name), "registry": name}


@router.get("/registries/{name}/versions")
def get_registry_versions(name: str):
    """Get available version tags for a registry."""
    if is_simulation():
        return {"versions": _simulated().mock_list_tags(name)}
    try:
        regs = list_registries()
        reg = next((r for r in regs if r["name"] == name), None)
        if not reg:
            raise HTTPException(status_code=404, detail=f"Registry '{name}' not found")
        url = reg.get("url", "")
        if not url:
            return {"versions": []}
        tags = _oras_list_tags(url, auth=reg.get("auth"))
        return {"versions": sorted(tags, reverse=True)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to get versions for registry %s: %s", name, exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Collections (Browse) ─────────────────────────────────────────────────────


@router.get("/collections")
def get_collections(
    registry: str = Query(None, description="Registry name to filter by"),
    version: str = Query(None, description="Version tag to filter by"),
):
    """List available recipe collections from one or more registries."""
    lister = _simulated().mock_list_collections if is_simulation() else list_collections
    try:
        collections = lister(registry_name=registry, version=version)
        return [
            {
                "name": c.name,
                "version": c.version,
                "display_version": c.display_version or c.version,
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
    lister = (
        _simulated().mock_list_collection_recipes
        if is_simulation()
        else list_collection_recipes
    )
    try:
        recipes = lister(collection_name=name, version=version, registry_name=registry)
        return [
            {
                "name": r.name,
                "description": r.description or "",
                "model": r.model or "",
                "container": r.container or "",
                "recipe_version": r.recipe_version or "",
                "solo_only": r.solo_only,
                "cluster_only": r.cluster_only,
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
    name = body.get("name")
    version = body.get("version")
    registry = body.get("registry")

    if not name or not version:
        raise HTTPException(
            status_code=400,
            detail="name and version are required",
        )

    installer = (
        _simulated().mock_install_collection if is_simulation() else install_collection
    )
    try:
        installed = installer(
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
    collection = body.get("collection")
    recipe = body.get("recipe")
    version = body.get("version")
    registry = body.get("registry")
    overwrite = body.get("overwrite", False)

    if not collection or not recipe:
        raise HTTPException(
            status_code=400, detail="collection and recipe are required"
        )

    installer = (
        _simulated().mock_install_oci_recipe if is_simulation() else install_oci_recipe
    )
    try:
        return installer(
            collection_name=collection,
            recipe_name=recipe,
            version=version or "",
            registry_name=registry,
            overwrite=overwrite,
        )
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
    collection = body.get("collection")
    version = body.get("version")
    registry = body.get("registry")

    if not collection:
        raise HTTPException(status_code=400, detail="collection is required")

    updater = (
        _simulated().mock_update_oci_recipe if is_simulation() else update_oci_recipe
    )
    try:
        return updater(
            recipe_name=recipe_name,
            collection_name=collection,
            version=version,
            registry_name=registry,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Recipe update failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/recipes/{recipe_name}")
def uninstall_oci_recipe_endpoint(recipe_name: str):
    """Uninstall an OCI-installed recipe."""
    result = uninstall_oci_recipe(recipe_name)
    if not result["success"]:
        raise HTTPException(status_code=404, detail=f"Recipe '{recipe_name}' not found")
    return result


# ── Update Check ─────────────────────────────────────────────────────────────


@router.get("/check")
def get_update_check(
    collection: str = Query(None, description="Filter by collection name"),
    registry: str = Query(None, description="Filter by registry name"),
):
    """Check for available updates for installed OCI recipes."""
    checker = _simulated().mock_check_updates if is_simulation() else check_updates
    try:
        updates = checker(collection=collection, registry=registry)
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
    lister = _simulated().mock_list_oci_recipes if is_simulation() else list_oci_recipes
    metas = lister()
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


@router.post("/cache/clear")
def clear_cache_endpoint(body: dict = {}):
    """Clear OCI meta cache. Optionally clear a specific cache entry."""
    key = body.get("key")
    result = clear_oci_cache(key)
    return result


@router.post("/background/start")
def start_background_endpoint():
    """Start the background update checker."""
    start_background_updater()
    return {"started": True}


@router.post("/background/stop")
def stop_background_endpoint():
    """Stop the background update checker."""
    stop_background_updater()
    return {"stopped": True}
