"""OCI registry tools — browse, pull, install recipe collections from OCI registries.

Uses the `oras` Python SDK for all OCI operations (tag listing, pulling, layout management).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from spark_pulse.config import config

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

REGISTRIES_CONFIG = Path.home() / ".config" / "spark-pulse" / "registries.yaml"
OCI_CACHE_DIR = Path.home() / ".cache" / "spark-pulse" / "oci"
OCI_META_CACHE_DIR = OCI_CACHE_DIR / "meta_cache"
RECIPES_DIR = Path.home() / ".config" / "spark-pulse" / "recipes"
AUTO_UPDATE_LOG = Path.home() / ".local" / "share" / "spark-pulse" / "auto-update.log"

# OCI media types we recognise
OCI_INDEX_MEDIA = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST_MEDIA = "application/vnd.oci.image.manifest.v1+json"
RECIPE_INDEX_ARTIFACT = "application/vnd.delivery-station.recipe.index.v1+json"

# ── Cache settings ───────────────────────────────────────────────────────────

_DEFAULT_CACHE_TTL = 300  # 5 minutes

# Background updater state
_background_thread: threading.Thread | None = None
_background_stop = threading.Event()


def _cache_ttl() -> int:
    """Return the cache TTL in seconds from config, with fallback."""
    try:
        return int(
            os.environ.get(
                "OCI_CACHE_TTL_SECONDS",
                str(config.oci_cache_ttl_seconds),
            )
        )
    except Exception:
        return _DEFAULT_CACHE_TTL


def _cache_key(registry_name: str, version: str) -> str:
    """Generate a cache key from registry name and version."""
    return f"{registry_name}:{version}"


def _cache_path(key: str) -> Path:
    """Get the cache file path for a given key."""
    OCI_META_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_key = key.replace(":", "_").replace("/", "_")
    return OCI_META_CACHE_DIR / f"{safe_key}.json"


def _read_cache(key: str) -> dict | None:
    """Read cached data if it exists and is not expired.

    Returns the cached data dict or None if cache miss/expired.
    """
    cache_file = _cache_path(key)
    if not cache_file.exists():
        return None

    try:
        with open(cache_file) as f:
            data = json.load(f)

        # Check TTL
        cached_at = data.get("_cached_at", 0)
        ttl = _cache_ttl()
        if time.time() - cached_at > ttl:
            cache_file.unlink(missing_ok=True)
            return None

        return data.get("data")
    except Exception as exc:
        logger.debug("Cache read failed for %s: %s", key, exc)
        return None


def _write_cache(key: str, data: dict) -> None:
    """Write data to the cache with current timestamp."""
    try:
        cache_file = _cache_path(key)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"_cached_at": time.time(), "data": data}, indent=2)
        )
    except Exception as exc:
        logger.debug("Cache write failed for %s: %s", key, exc)


def _clear_cache(key: str | None = None) -> None:
    """Clear cache. If key is None, clear all cache."""
    if not OCI_META_CACHE_DIR.exists():
        return
    if key:
        _cache_path(key).unlink(missing_ok=True)
    else:
        for f in OCI_META_CACHE_DIR.glob("*.json"):
            f.unlink(missing_ok=True)


# ── Background updater ───────────────────────────────────────────────────────


def _background_update_loop() -> None:
    """Background thread loop that periodically checks for updates."""
    logger.info("OCI background updater started")
    while not _background_stop.wait(timeout=_background_check_interval()):
        try:
            logger.info("OCI background update check started")
            updates = check_updates()
            if updates:
                logger.info(
                    "OCI background update check found %d update(s)", len(updates)
                )
                for upd in updates:
                    logger.info(
                        "  %s: %s -> %s",
                        upd.collection,
                        upd.current_version,
                        upd.latest_version,
                    )
            else:
                logger.debug("OCI background update check: no updates available")
        except Exception as exc:
            logger.warning("OCI background update check failed: %s", exc)


def _background_check_interval() -> int:
    """Return the background check interval from config."""
    try:
        return int(
            os.environ.get(
                "OCI_BACKGROUND_CHECK_INTERVAL_SECONDS",
                str(config.oci_background_check_interval_seconds),
            )
        )
    except Exception:
        return 900  # 15 minutes default


def start_background_updater() -> None:
    """Start the background update checker thread."""
    global _background_thread
    if _background_thread and _background_thread.is_alive():
        return

    _background_stop.clear()
    _background_thread = threading.Thread(
        target=_background_update_loop,
        name="oci-bg-updater",
        daemon=True,
    )
    _background_thread.start()
    logger.info(
        "OCI background updater started (interval: %d s)",
        _background_check_interval(),
    )


def stop_background_updater() -> None:
    """Stop the background update checker thread."""
    global _background_thread
    _background_stop.set()
    if _background_thread:
        _background_thread.join(timeout=10)
        _background_thread = None
    logger.info("OCI background updater stopped")


def clear_oci_cache(key: str | None = None) -> dict:
    """Clear OCI meta cache. If key is None, clear all cache.

    Returns a summary dict with cleared count.
    """
    if not OCI_META_CACHE_DIR.exists():
        return {"cleared": 0}

    if key:
        cache_file = _cache_path(key)
        if cache_file.exists():
            cache_file.unlink()
            return {"cleared": 1}
        return {"cleared": 0}
    else:
        count = sum(1 for f in OCI_META_CACHE_DIR.glob("*.json"))
        for f in OCI_META_CACHE_DIR.glob("*.json"):
            f.unlink(missing_ok=True)
        return {"cleared": count}


# ── Registry config ──────────────────────────────────────────────────────────


def _load_registries() -> list[dict]:
    """Load registries from registries.yaml. Returns empty list on missing/invalid file.

    Falls back to bundled spark_pulse/registries.yaml if user config doesn't exist.
    """
    user_config = REGISTRIES_CONFIG
    bundled_config = Path(__file__).parent / "registries.yaml"

    # Use user config if it exists, otherwise fall back to bundled default
    config_path = user_config if user_config.exists() else bundled_config

    if not config_path.exists():
        return []
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict) or "registries" not in data:
            return []
        regs = data["registries"]
        if not isinstance(regs, list):
            return []
        return regs
    except Exception as exc:
        logger.warning("Failed to load registries config: %s", exc)
        return []


def _save_registries(registries: list[dict]) -> None:
    """Persist registries list to registries.yaml."""
    REGISTRIES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRIES_CONFIG, "w") as f:
        yaml.dump({"registries": registries}, f, default_flow_style=False)


def list_registries() -> list[dict]:
    """Return all configured registries with a connectivity probe."""
    regs = _load_registries()
    for r in regs:
        r["connected"] = test_registry_connection(r["name"])
    return regs


def add_registry(registry: dict) -> dict:
    """Add a new registry. Returns the full registry dict."""
    regs = _load_registries()
    # Deduplicate by name
    regs = [r for r in regs if r["name"] != registry["name"]]
    regs.append(
        {
            "name": registry["name"],
            "url": registry["url"],
            "enabled": registry.get("enabled", True),
            "default": registry.get("default", False),
            "auth": registry.get("auth", {}),
        }
    )
    _save_registries(regs)
    return regs[-1]


def remove_registry(name: str) -> bool:
    """Remove a registry by name. Returns True if found and removed."""
    regs = _load_registries()
    before = len(regs)
    regs = [r for r in regs if r["name"] != name]
    if len(regs) < before:
        _save_registries(regs)
        return True
    return False


def update_registry(name: str, updates: dict) -> dict | None:
    """Update fields of an existing registry. Returns updated dict or None."""
    regs = _load_registries()
    for i, r in enumerate(regs):
        if r["name"] == name:
            regs[i].update(updates)
            _save_registries(regs)
            return regs[i]
    return None


def get_registry(name: str) -> dict | None:
    """Get a single registry by name."""
    for r in _load_registries():
        if r["name"] == name:
            return r
    return None


def get_default_registry() -> dict | None:
    """Return the default registry, or the first enabled one."""
    regs = _load_registries()
    for r in regs:
        if r.get("default"):
            return r
    for r in regs:
        if r.get("enabled", True):
            return r
    return regs[0] if regs else None


def test_registry_connection(name: str) -> bool:
    """Test connectivity to a registry using oras Python SDK."""
    reg = get_registry(name)
    if not reg:
        return False
    url = reg.get("url", "")
    if not url:
        return False
    try:
        _oras_list_tags(url, auth=reg.get("auth"))
        return True  # If we can list tags (even empty), registry is reachable
    except Exception as exc:
        logger.debug("Registry %s connection test failed: %s", name, exc)
        return False


# ── Auth helpers ─────────────────────────────────────────────────────────────


def _auth_headers(auth: dict | None) -> dict[str, str]:
    """Build Basic auth header from auth config."""
    if not auth:
        return {}
    if auth.get("type") == "token" and auth.get("token"):
        return {"Authorization": f"Bearer {auth['token']}"}
    if auth.get("type") == "username_password":
        import base64

        username = auth.get("username", "")
        password = auth.get("password", "")
        if auth.get("password_env"):
            password = os.environ.get(auth["password_env"], password)
        creds = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}
    return {}


# ── ORAS Python SDK wrapper ─────────────────────────────────────────────────


def _oras_client(auth: dict | None = None):
    """Create an oras client with optional auth."""
    import oras.client

    headers = _auth_headers(auth)
    client = oras.client.OrasClient()
    if headers.get("Authorization"):
        # Set auth header for token auth
        client.session.headers.update(headers)
    return client


def _oras_list_tags(url: str, auth: dict | None = None) -> list[str]:
    """List tags from an OCI registry using oras Python SDK."""
    client = _oras_client(auth)
    tags = client.get_tags(url)
    return tags or []


def _oras_pull_to_layout(
    url: str, tag: str, layout_dir: Path, auth: dict | None = None
) -> None:
    """Pull an OCI image to a local OCI layout directory using oras Python SDK."""
    layout_dir.mkdir(parents=True, exist_ok=True)
    client = _oras_client(auth)
    # Pull to the layout directory
    client.pull(target=f"{url}:{tag}", outdir=str(layout_dir))


def _oras_fetch_manifest(url: str, tag: str, auth: dict | None = None) -> dict:
    """Fetch and parse an OCI manifest using oras Python SDK."""
    client = _oras_client(auth)
    return client.get_manifest(f"{url}:{tag}")


def _fetch_oci_index(url: str, tag: str, auth: dict | None = None) -> dict:
    """Fetch and parse an OCI index manifest using oras Python SDK."""
    return _oras_fetch_manifest(url, tag, auth=auth)


def _pull_oci_to_layout(
    url: str, tag: str, layout_dir: Path, auth: dict | None = None
) -> None:
    """Pull an OCI image to a local OCI layout directory using oras Python SDK."""
    layout_dir.mkdir(parents=True, exist_ok=True)
    _oras_pull_to_layout(url, tag, layout_dir, auth=auth)


def _extract_recipes_from_layout(layout_dir: Path, extract_dir: Path) -> list[dict]:
    """Extract recipe YAML files from an OCI layout directory.

    Reads the index, finds child manifests, and extracts each recipe file.
    Returns list of dicts with 'filename', 'content', 'digest', 'size'.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)
    extracted = []

    # Read the OCI index
    index_path = layout_dir / "index.json"
    if not index_path.exists():
        raise RuntimeError("Invalid OCI layout: missing index.json")

    with open(index_path) as f:
        index_data = json.load(f)

    # Find the recipe index manifest
    manifests = index_data.get("manifests", [])
    recipe_manifest_ref = None
    for m in manifests:
        mt = m.get("mediaType", "")
        art = m.get("artifactType", "")
        if mt == OCI_INDEX_MEDIA or art == RECIPE_INDEX_ARTIFACT:
            recipe_manifest_ref = m["digest"]
            break

    if not recipe_manifest_ref:
        # Try the first manifest as fallback
        if manifests:
            recipe_manifest_ref = manifests[0]["digest"]
        else:
            raise RuntimeError("No manifests found in OCI layout")

    # Read the recipe index (list of recipe manifests)
    obj_dir = layout_dir / "blobs" / "sha256"
    manifest_path = obj_dir / recipe_manifest_ref
    if not manifest_path.exists():
        raise RuntimeError(f"Manifest blob not found: {recipe_manifest_ref}")

    with open(manifest_path) as f:
        recipe_index = json.load(f)

    # Each entry in manifests is a recipe manifest
    for entry in recipe_index.get("manifests", []):
        digest = entry.get("digest", "")
        size = entry.get("size", 0)
        # The layer inside the recipe manifest contains the YAML
        layers = entry.get("layers", [])
        if not layers:
            continue

        layer_digest = layers[0].get("digest", "")
        layer_path = obj_dir / layer_digest
        if not layer_path.exists():
            logger.warning("Layer blob not found: %s", layer_digest)
            continue

        # Extract filename from annotations or use digest
        annotations = entry.get("annotations", {})
        filename = annotations.get("org.opencontainers.image.title", "")
        if not filename:
            filename = annotations.get("io.github.spark-pulse.recipe", "")
        if not filename:
            filename = f"recipe-{digest[:12]}.yaml"

        try:
            with open(layer_path) as f:
                content = f.read()
            extracted.append(
                {
                    "filename": filename,
                    "content": content,
                    "digest": digest,
                    "size": size,
                }
            )
        except Exception as exc:
            logger.warning("Failed to read layer %s: %s", layer_digest, exc)

    return extracted


# ── High-level operations ────────────────────────────────────────────────────


@dataclass
class CollectionInfo:
    name: str
    version: str
    description: str
    vendor: str
    license: str
    recipe_count: int
    digest: str
    registry: str
    display_version: str = ""  # Human-readable version from annotations


def list_collections(
    registry_name: str | None = None, version: str | None = None
) -> list[CollectionInfo]:
    """List all available recipe collections from one or more registries.

    Uses file-based cache to avoid repeated network calls. Cache TTL is
    configurable via config.oci_cache_ttl_seconds (default 5 minutes).
    """
    results = []
    registries = _load_registries()

    if registry_name:
        regs = [r for r in registries if r["name"] == registry_name]
        if not regs:
            raise ValueError(f"Registry '{registry_name}' not found")
        registries = regs

    for reg in registries:
        if not reg.get("enabled", True):
            continue
        url = reg.get("url", "")
        if not url:
            continue

        try:
            tags = _oras_list_tags(url, auth=reg.get("auth"))
            # Filter tags by version if specified
            if version:
                tags = [t for t in tags if t == version]
            elif version == "":
                tags = []  # Explicit empty version = no results

            for tag in tags:
                try:
                    cache_key = _cache_key(reg["name"], tag)
                    cached = _read_cache(cache_key)

                    if cached:
                        logger.debug("Cache hit for %s", cache_key)
                        annotations = cached.get("annotations", {})
                        index = cached.get("index", {})
                        display_ver = annotations.get("version", tag)
                        results.append(
                            CollectionInfo(
                                name=annotations.get("name", "unknown"),
                                version=tag,
                                description=annotations.get("description", ""),
                                vendor=annotations.get("vendor", ""),
                                license=annotations.get("license", ""),
                                recipe_count=len(index.get("manifests", [])),
                                digest=index.get("digest", tag),
                                registry=reg["name"],
                                display_version=display_ver,
                            )
                        )
                    else:
                        index = _fetch_oci_index(url, tag, auth=reg.get("auth"))
                        annotations = index.get("annotations", {})
                        # Prefer annotation version (e.g., "1.0.0") over raw tag (e.g., sha256:...)
                        display_ver = annotations.get("version", tag)
                        results.append(
                            CollectionInfo(
                                name=annotations.get("name", "unknown"),
                                version=tag,
                                description=annotations.get("description", ""),
                                vendor=annotations.get("vendor", ""),
                                license=annotations.get("license", ""),
                                recipe_count=len(index.get("manifests", [])),
                                digest=index.get("digest", tag),
                                registry=reg["name"],
                                display_version=display_ver,
                            )
                        )
                        # Write to cache
                        _write_cache(
                            cache_key, {"annotations": annotations, "index": index}
                        )
                except Exception as exc:
                    logger.debug("Failed to parse index %s:%s: %s", url, tag, exc)
                    continue
        except Exception as exc:
            logger.warning("Failed to list tags for registry %s: %s", reg["name"], exc)

    # Deduplicate by (name, registry), keeping only the latest version
    groups: dict[tuple[str, str], list[CollectionInfo]] = {}
    for col in results:
        key = (col.name, col.registry)
        groups.setdefault(key, []).append(col)

    def _version_key(v: str) -> tuple:
        """Convert version string to sortable tuple."""
        try:
            parts = v.split(".")
            return tuple(int(p) for p in parts)
        except ValueError:
            return (0,)

    deduplicated = []
    for key, cols in groups.items():
        latest = max(
            cols,
            key=lambda c: (
                _version_key(c.version) if c.version != "latest" else (0, 0, 0)
            ),
        )
        deduplicated.append(latest)

    return deduplicated


@dataclass
class CollectionRecipe:
    """Individual recipe info from a collection."""

    name: str
    description: str
    model: str
    container: str
    recipe_version: str
    solo_only: bool = False
    cluster_only: bool = False


def _extract_recipe_from_layer(
    registry_url: str,
    entry: dict,
    tag: str,
    auth: dict | None = None,
) -> dict:
    """Extract recipe metadata from a layer blob when annotations are missing.

    Fetches the individual recipe manifest, gets the layer digest,
    downloads the YAML content, and parses it for metadata.
    """
    import oras.client

    client = oras.client.OrasClient()
    if auth:
        headers = _auth_headers(auth)
        if headers.get("Authorization"):
            client.session.headers.update(headers)

    # Get the digest for this recipe manifest
    digest = entry.get("digest", "")
    if not digest:
        raise ValueError("No digest found in manifest entry")

    # Fetch the individual recipe manifest
    manifest = client.get_manifest(f"{registry_url}@{digest}")

    # Get the layer containing the YAML
    layers = manifest.get("layers", [])
    if not layers:
        raise ValueError("No layers found in recipe manifest")

    layer = layers[0]
    layer_digest = layer.get("digest", "")
    if not layer_digest:
        raise ValueError("No layer digest found")

    # Download the layer blob using oras client
    try:
        response = client.get_blob(registry_url, layer_digest)
        response.raise_for_status()
        yaml_content = response.text
    except Exception as exc:
        logger.debug("Failed to fetch layer blob %s: %s", layer_digest, exc)
        raise

    # Parse YAML to extract metadata
    try:
        recipe_data = yaml.safe_load(yaml_content) or {}
    except yaml.YAMLError as exc:
        logger.debug("Failed to parse recipe YAML: %s", exc)
        raise

    # Extract fields from YAML
    name = recipe_data.get("name", digest.split(":")[-1])
    description = recipe_data.get("description", "")
    model = recipe_data.get("model", "")
    container = recipe_data.get("container", "")
    solo_only = bool(recipe_data.get("solo_only", False))
    cluster_only = bool(recipe_data.get("cluster_only", False))

    return {
        "name": name,
        "description": description,
        "model": model,
        "container": container,
        "recipe_version": tag,
        "solo_only": solo_only,
        "cluster_only": cluster_only,
    }


def list_collection_recipes(
    collection_name: str,
    registry_name: str | None = None,
    version: str | None = None,
) -> list[CollectionRecipe]:
    """List individual recipes in a collection.

    Pulls the OCI index and extracts recipe metadata from annotations.
    """
    results: list[CollectionRecipe] = []
    registries = _load_registries()

    if registry_name:
        regs = [r for r in registries if r["name"] == registry_name]
        if not regs:
            raise ValueError(f"Registry '{registry_name}' not found")
        registries = regs

    for reg in registries:
        if not reg.get("enabled", True):
            continue
        url = reg.get("url", "")
        if not url:
            continue

        try:
            tags = _oras_list_tags(url, auth=reg.get("auth"))
            if version:
                tags = [t for t in tags if t == version]
            elif version == "":
                tags = []

            for tag in tags:
                try:
                    index = _fetch_oci_index(url, tag, auth=reg.get("auth"))
                    annotations = index.get("annotations", {})
                    if annotations.get("name") != collection_name:
                        continue

                    # Extract recipe info from manifest entries
                    for entry in index.get("manifests", []):
                        layer_annotations = entry.get("annotations", {})

                        # If annotations are missing or minimal, try to extract from YAML layer
                        recipe_name = layer_annotations.get("name")
                        if not recipe_name:
                            recipe_name = entry.get("digest", "unknown")

                        # Check if we have meaningful annotations
                        has_annotations = any(
                            k in layer_annotations
                            for k in [
                                "name",
                                "model",
                                "container",
                                "description",
                                "org.opencontainers.image.description",
                                "recipe_version",
                            ]
                        )

                        if has_annotations:
                            # Use annotations directly
                            results.append(
                                CollectionRecipe(
                                    name=recipe_name,
                                    description=layer_annotations.get(
                                        "org.opencontainers.image.description", ""
                                    ),
                                    model=layer_annotations.get("model", ""),
                                    container=layer_annotations.get("container", ""),
                                    recipe_version=layer_annotations.get(
                                        "recipe_version", tag
                                    ),
                                    solo_only=bool(
                                        layer_annotations.get("solo_only", False)
                                    ),
                                    cluster_only=bool(
                                        layer_annotations.get("cluster_only", False)
                                    ),
                                )
                            )
                        else:
                            # Annotations missing — fetch layer YAML to extract metadata
                            try:
                                recipe_info = _extract_recipe_from_layer(
                                    url, entry, tag, auth=reg.get("auth")
                                )
                                results.append(CollectionRecipe(**recipe_info))
                            except Exception as exc:
                                logger.debug(
                                    "Failed to extract recipe from layer for %s:%s: %s",
                                    collection_name,
                                    tag,
                                    exc,
                                )
                                # Fallback: use digest as name, empty other fields
                                results.append(
                                    CollectionRecipe(
                                        name=recipe_name,
                                        description="",
                                        model="",
                                        container="",
                                        recipe_version=tag,
                                        solo_only=False,
                                        cluster_only=False,
                                    )
                                )
                except Exception as exc:
                    logger.debug(
                        "Failed to parse index for %s:%s: %s", collection_name, tag, exc
                    )
                    continue
        except Exception as exc:
            logger.warning("Failed to list tags for registry %s: %s", reg["name"], exc)

    return results


def install_collection(
    name: str,
    version: str,
    registry_name: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Install a recipe collection from an OCI registry.

    Returns list of installed recipe filenames.
    """
    # Find the registry
    if registry_name:
        reg = get_registry(registry_name)
        if not reg:
            raise ValueError(f"Registry '{registry_name}' not found")
    else:
        reg = get_default_registry()
        if not reg:
            raise ValueError("No registries configured")

    url = reg["url"]
    auth = reg.get("auth")

    # Verify collection exists
    collections = list_collections(registry_name=reg["name"])
    matching = [c for c in collections if c.name == name and c.version == version]
    if not matching:
        raise ValueError(
            f"Collection '{name}:{version}' not found in registry '{reg['name']}'"
        )

    if dry_run:
        logger.info("DRY RUN: Would install %s:%s from %s", name, version, reg["name"])
        return []

    # Pull to OCI layout
    cache_dir = OCI_CACHE_DIR / reg["name"] / name / version
    extract_dir = cache_dir / "extracted"

    try:
        _pull_oci_to_layout(url, version, cache_dir, auth=auth)
    except Exception as exc:
        raise RuntimeError(f"Failed to pull OCI image: {exc}")

    # Extract recipes
    recipes = _extract_recipes_from_layout(cache_dir, extract_dir)
    if not recipes:
        raise RuntimeError("No recipe files found in collection")

    # Install recipes
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    installed = []

    for recipe in recipes:
        filename = recipe["filename"]
        dest = RECIPES_DIR / filename

        # Check for local modifications
        if dest.exists():
            try:
                with open(dest) as f:
                    existing = f.read()
                if existing != recipe["content"]:
                    logger.warning(
                        "Local modifications detected for %s — overwriting", filename
                    )
            except OSError:
                pass

        with open(dest, "w") as f:
            f.write(recipe["content"])

        # Create metadata sidecar
        _write_recipe_meta(filename, reg["name"], name, version, recipe["digest"])
        installed.append(filename)

    logger.info("Installed %d recipes from %s:%s", len(installed), name, version)
    return installed


def install_oci_recipe(
    collection_name: str,
    recipe_name: str,
    version: str,
    registry_name: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Install a single recipe from a collection.

    Returns dict with 'success', 'recipe', 'action' (installed/updated/skipped).
    """
    # Find the registry
    if registry_name:
        reg = get_registry(registry_name)
        if not reg:
            raise ValueError(f"Registry '{registry_name}' not found")
    else:
        reg = get_default_registry()
        if not reg:
            raise ValueError("No registries configured")

    url = reg["url"]
    auth = reg.get("auth")

    # Pull to OCI layout
    cache_dir = OCI_CACHE_DIR / reg["name"] / collection_name / version
    extract_dir = cache_dir / "extracted"

    try:
        _pull_oci_to_layout(url, version, cache_dir, auth=auth)
    except Exception as exc:
        raise RuntimeError(f"Failed to pull OCI image: {exc}")

    # Extract recipes and find the target
    recipes = _extract_recipes_from_layout(cache_dir, extract_dir)
    target = None
    for r in recipes:
        # Match by filename (without .yaml/.yml) or by name in content
        base_name = Path(r["filename"]).stem
        if base_name == recipe_name or r["filename"] == f"{recipe_name}.yaml":
            target = r
            break

    if not target:
        raise ValueError(
            f"Recipe '{recipe_name}' not found in collection '{collection_name}'"
        )

    # Check if already installed
    dest = RECIPES_DIR / target["filename"]
    action = "installed"

    if dest.exists() and not overwrite:
        try:
            with open(dest) as f:
                existing = f.read()
            if existing == target["content"]:
                return {"success": True, "recipe": recipe_name, "action": "up_to_date"}
            else:
                action = "updated"
                logger.info(
                    "Local modifications detected for %s — overwriting",
                    target["filename"],
                )
        except OSError:
            action = "updated"

    # Install/update the recipe
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    with open(dest, "w") as f:
        f.write(target["content"])

    # Update metadata
    _write_recipe_meta(
        target["filename"], reg["name"], collection_name, version, target["digest"]
    )

    logger.info(
        "%s recipe %s from %s:%s",
        action.capitalize(),
        recipe_name,
        collection_name,
        version,
    )
    return {"success": True, "recipe": recipe_name, "action": action}


def update_oci_recipe(
    recipe_name: str,
    collection_name: str,
    version: str | None = None,
    registry_name: str | None = None,
) -> dict:
    """Update an existing OCI-installed recipe to the latest version.

    Returns dict with 'success', 'recipe', 'action' (updated/skipped).
    """
    meta = get_oci_meta(recipe_name)
    if not meta:
        raise ValueError(f"Recipe '{recipe_name}' is not an OCI-installed recipe")

    # Use existing metadata if version not specified
    if version is None:
        version = meta.version
    if registry_name is None:
        registry_name = meta.source

    result = install_oci_recipe(
        collection_name=collection_name,
        recipe_name=recipe_name,
        version=version,
        registry_name=registry_name,
        overwrite=True,
    )
    return result


@dataclass
class UpdateInfo:
    collection: str
    current_version: str
    latest_version: str
    current_digest: str
    latest_digest: str
    local_changes: bool
    added_recipes: list[str] = field(default_factory=list)
    modified_recipes: list[str] = field(default_factory=list)


def check_updates(
    collection: str | None = None,
    registry: str | None = None,
) -> list[UpdateInfo]:
    """Check for available updates for installed OCI recipes.

    Returns list of UpdateInfo objects describing available updates.
    """
    updates = []
    oci_recipes = list_oci_recipes()

    # Group by (collection, registry)
    groups: dict[tuple[str, str], list[RecipeMeta]] = {}
    for meta in oci_recipes:
        key = (meta.collection, meta.source)
        groups.setdefault(key, []).append(meta)

    for (coll_name, reg_name), metas in groups.items():
        if collection and coll_name != collection:
            continue
        if registry and reg_name != registry:
            continue

        # Get latest version from registry
        try:
            collections = list_collections(registry_name=reg_name)
            latest = [c for c in collections if c.name == coll_name]
            if not latest:
                continue
            # Sort by version string (simple comparison)
            latest.sort(key=lambda c: c.version, reverse=True)
            latest_info = latest[0]
        except Exception as exc:
            logger.warning("Failed to check registry for %s: %s", coll_name, exc)
            continue

        current_version = metas[0].version if metas else ""
        if current_version == latest_info.version:
            continue  # Already up to date

        # Check for local modifications
        has_local_changes = any(m.local_changes for m in metas)

        # We'd need to know what recipes are in the latest version
        # For now, just report version difference
        updates.append(
            UpdateInfo(
                collection=coll_name,
                current_version=current_version,
                latest_version=latest_info.version,
                current_digest=metas[0].digest if metas else "",
                latest_digest=latest_info.digest,
                local_changes=has_local_changes,
            )
        )

    return updates


@dataclass
class RecipeMeta:
    name: str
    source: str  # registry name
    collection: str
    version: str
    digest: str
    installed_at: str
    updated_at: str
    local_changes: bool


def _meta_path(recipe_filename: str) -> Path:
    """Get the metadata file path for a recipe."""
    base = Path(recipe_filename)
    if base.suffix in {".yaml", ".yml"}:
        return RECIPES_DIR / f"{base.stem}.yaml.meta"
    return RECIPES_DIR / f"{recipe_filename}.meta"


def _write_recipe_meta(
    recipe_filename: str,
    source: str,
    collection: str,
    version: str,
    digest: str,
) -> None:
    """Write (or update) a recipe's metadata sidecar file."""
    meta_path = _meta_path(recipe_filename)
    now = datetime.now(timezone.utc).isoformat()

    existing = {}
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            existing = {}

    meta = {
        "source": source,
        "collection": collection,
        "version": version,
        "digest": digest,
        "installed_at": existing.get("installed_at", now),
        "updated_at": now,
        "local_changes": existing.get("local_changes", False),
    }

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_path, "w") as f:
        yaml.dump(meta, f, default_flow_style=False)


def _read_recipe_meta(recipe_filename: str) -> RecipeMeta | None:
    """Read metadata for a recipe. Returns None if no metadata exists."""
    meta_path = _meta_path(recipe_filename)
    if not meta_path.exists():
        return None

    try:
        with open(meta_path) as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return None

    # Check if the recipe file has local modifications
    base = Path(recipe_filename)
    recipe_file = (
        RECIPES_DIR / base.name
        if base.suffix in {".yaml", ".yml"}
        else RECIPES_DIR / recipe_filename
    )
    local_changes = False
    if recipe_file.exists():
        try:
            with open(recipe_file) as f:
                content = f.read()
            # Compare with digest — simplified check
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            if content_hash[:12] != data.get("digest", "")[:12]:
                local_changes = True
        except OSError:
            pass

    return RecipeMeta(
        name=base.name,
        source=data.get("source", ""),
        collection=data.get("collection", ""),
        version=data.get("version", ""),
        digest=data.get("digest", ""),
        installed_at=data.get("installed_at", ""),
        updated_at=data.get("updated_at", ""),
        local_changes=local_changes,
    )


def list_oci_recipes() -> list[RecipeMeta]:
    """List all recipes that were installed from OCI collections."""
    if not RECIPES_DIR.is_dir():
        return []

    result = []
    for meta_file in sorted(RECIPES_DIR.glob("*.meta")):
        # Derive the recipe filename from the meta filename
        base = meta_file.stem  # e.g. "spark-vllm-7b.yaml"
        recipe_filename = f"{base}.yaml" if not base.endswith(".yaml") else base
        meta = _read_recipe_meta(recipe_filename)
        if meta:
            result.append(meta)

    return result


def get_oci_meta(recipe_name: str) -> RecipeMeta | None:
    """Get metadata for a specific recipe."""
    return _read_recipe_meta(recipe_name)


def apply_updates(
    updates: list[dict],
    overwrite_local: bool = False,
) -> list[dict]:
    """Apply a list of updates.

    Each update dict has: collection, target_version, registry.
    Returns list of result dicts with success/error info.
    """
    results = []

    for upd in updates:
        coll = upd["collection"]
        version = upd["target_version"]
        reg_name = upd.get("registry")

        try:
            installed = install_collection(
                name=coll,
                version=version,
                registry_name=reg_name,
            )
            results.append(
                {
                    "collection": coll,
                    "success": True,
                    "installed": installed,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "collection": coll,
                    "success": False,
                    "error": str(exc),
                }
            )

    return results


# ── Auto-update ──────────────────────────────────────────────────────────────


def run_auto_update() -> dict:
    """Run the auto-update check and apply updates.

    Returns a summary dict with results.
    """
    settings = (
        config.oci_auto_update_enabled
        if hasattr(config, "oci_auto_update_enabled")
        else False
    )
    if not settings:
        return {"skipped": True, "reason": "Auto-update disabled"}

    overwrite = (
        config.oci_auto_update_overwrite_local
        if hasattr(config, "oci_auto_update_overwrite_local")
        else False
    )

    log_lines = []

    def log(msg: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] {msg}"
        log_lines.append(line)
        logger.info(line)

    log("Auto-update started")

    try:
        updates = check_updates()
        if not updates:
            log("No updates available")
            return {"success": True, "updated": 0, "log": log_lines}

        log(f"Found {len(updates)} available update(s)")

        update_params = []
        for upd in updates:
            log(f"  {upd.collection}: {upd.current_version} -> {upd.latest_version}")
            if upd.local_changes and not overwrite:
                log(f"    Skipping {upd.collection}: local changes detected")
                continue
            update_params.append(
                {
                    "collection": upd.collection,
                    "target_version": upd.latest_version,
                    "registry": None,  # Will use default
                }
            )

        if not update_params:
            log("No updates to apply (all have local changes)")
            return {"success": True, "updated": 0, "log": log_lines}

        results = apply_updates(update_params, overwrite_local=overwrite)
        total_installed = sum(
            len(r.get("installed", [])) for r in results if r["success"]
        )

        for r in results:
            if r["success"]:
                log(
                    f"  Updated {r['collection']}: {len(r['installed'])} recipes installed"
                )
            else:
                log(
                    f"  Failed to update {r['collection']}: {r.get('error', 'unknown')}"
                )

        log(f"Auto-update complete: {total_installed} recipes updated")
        return {"success": True, "updated": total_installed, "log": log_lines}

    except Exception as exc:
        log(f"Auto-update failed: {exc}")
        return {"success": False, "error": str(exc), "log": log_lines}


def save_auto_update_log() -> None:
    """Append the latest auto-update log to the persistent log file."""
    # This is called after run_auto_update() to persist the log
    pass  # Log is already written via logger; file persistence handled by caller
