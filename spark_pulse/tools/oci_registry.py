"""OCI registry tools — browse, pull, install recipe collections from OCI registries.

Uses the `oras` Python SDK for all OCI operations (tag listing, pulling, layout management).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from spark_pulse.config import config
from spark_pulse.tools.atomic_json import write_text_atomic

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

REGISTRIES_CONFIG = Path.home() / ".config" / "spark-pulse" / "registries.yaml"
#: The default registry list shipped inside the package, one directory up from
#: this module. Used verbatim when the user has never written their own config.
BUNDLED_REGISTRIES_CONFIG = Path(__file__).resolve().parent.parent / "registries.yaml"
OCI_CACHE_DIR = Path.home() / ".cache" / "spark-pulse" / "oci"
OCI_META_CACHE_DIR = OCI_CACHE_DIR / "meta_cache"
#: Where an installed OCI recipe lands. ``recipe_sources`` lists this directory
#: directly, under ``oci-<stem>`` ids.
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
        write_text_atomic(
            cache_file,
            json.dumps({"_cached_at": time.time(), "data": data}, indent=2),
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
    bundled_config = BUNDLED_REGISTRIES_CONFIG

    # Use user config if it exists, otherwise fall back to bundled default
    config_path = user_config if user_config.exists() else bundled_config

    if not config_path.exists():
        return []

    regs = _read_registries_file(config_path)
    if regs is not None:
        return regs

    # An unusable file yields no registries, and says so loudly.
    #
    # Falling back to the bundled defaults was the other candidate and is the
    # wrong one: an operator who configured only their own private registry
    # would then find a corrupted file had silently substituted the public
    # default, and recipes would start being pulled from somewhere they did
    # not choose. Doing nothing is the safe failure for a source list. What
    # was actually wrong here is that the failure was *silent* — and that the
    # torn write which caused it is no longer possible, because the write is
    # atomic now.
    logger.error(
        "%s exists but holds no usable registry list, so no registries are "
        "configured. Inspect or remove the file; a backup of a working list "
        "is the only way back to one.",
        config_path,
    )
    return []


def _read_registries_file(path: Path) -> list[dict] | None:
    """The registry list in ``path``, or None when the file is unusable.

    None means "this file did not tell us anything", and is deliberately
    distinct from ``[]``, which means "this file says there are none".
    """
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except Exception as exc:
        logger.warning("Failed to load registries config %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("registries"), list):
        return None
    return data["registries"]


def _save_registries(registries: list[dict]) -> None:
    """Persist registries list to registries.yaml, atomically.

    A plain ``open(path, "w")`` truncates before it writes, so a crash, a full
    disk or a power loss between those two moments leaves the operator's
    registry list empty or half-written — and YAML makes that worse rather
    than better, because a torn file usually still parses into a plausible
    wrong value instead of failing loudly.
    """
    write_text_atomic(
        REGISTRIES_CONFIG,
        yaml.dump({"registries": registries}, default_flow_style=False),
    )


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
    """Update fields of an existing registry. Returns updated dict or None.

    ``auth`` is merged into the existing auth dict rather than replacing it
    wholesale: the browser never receives a stored secret back (see
    ``routers/oci.py``), so an edit that only changes the URL or the username
    must not be able to see, and therefore cannot resend, the token or
    password already on file. A partial ``auth`` update (e.g. just a new
    ``token``, or just a changed ``username``) keeps whatever it does not
    mention.
    """
    regs = _load_registries()
    for i, r in enumerate(regs):
        if r["name"] == name:
            auth_update = updates.pop("auth", None)
            regs[i].update(updates)
            if auth_update is not None:
                merged_auth = dict(regs[i].get("auth") or {})
                # A masked secret is what the router *returns*; it is never a
                # value to store. A client that reads a registry and writes it
                # back verbatim must keep the real credential, not replace it
                # with bullets — so masked values are dropped from the update.
                merged_auth.update(
                    {
                        k: v
                        for k, v in auth_update.items()
                        if not (k in _SECRET_AUTH_KEYS and _is_masked_secret(v))
                    }
                )
                regs[i]["auth"] = merged_auth
            _save_registries(regs)
            return regs[i]
    return None


#: Auth keys that hold a credential. Kept here, beside the merge that must
#: refuse a masked one, and mirrored by the router's masking of responses.
_SECRET_AUTH_KEYS = ("token", "password")
_MASK_CHAR = "•"


def _is_masked_secret(value: object) -> bool:
    """Whether ``value`` is the masked marker a response carries, not a secret."""
    return isinstance(value, str) and value.startswith(_MASK_CHAR)


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


def _safe_layer_filename(raw: str, layer_digest: str) -> str:
    """Reduce a registry-supplied layer title to a name safe to join to a dir.

    Layer annotations come from a remote registry, so they can carry path
    separators (``../../.ssh/authorized_keys``) or be absolute. Anything that is
    not a plain file name is discarded in favour of a digest-derived name.
    """
    candidate = Path(raw.strip()).name if raw else ""
    if candidate in {"", ".", ".."} or "/" in candidate or "\\" in candidate:
        candidate = ""
    if not candidate:
        candidate = f"recipe-{(layer_digest or 'unknown')[:12]}.yaml"
    return candidate


def _oras_pull_to_layout(
    url: str, tag: str, layout_dir: Path, auth: dict | None = None
) -> None:
    """Pull recipe YAML files from an OCI index artifact.

    The artifact structure is:
      index (tagged) → manifests[] → recipe manifests → layers[] → YAML files

    The oras SDK's pull() only works for flat artifacts with direct layers,
    so we manually traverse the index and download each recipe's YAML layer.
    """
    layout_dir.mkdir(parents=True, exist_ok=True)
    client = _oras_client(auth)

    # Fetch the index manifest
    index = client.get_manifest(f"{url}:{tag}")

    # Process each recipe manifest in the index
    for recipe_ref in index.get("manifests", []):
        recipe_digest = recipe_ref.get("digest", "")
        if not recipe_digest:
            continue

        # Fetch the recipe manifest by digest
        recipe_url = f"{url}@{recipe_digest}"
        try:
            recipe_manifest = client.get_manifest(recipe_url)
        except Exception as exc:
            logger.warning(
                "Failed to fetch recipe manifest %s: %s", recipe_digest[:16], exc
            )
            continue

        # Download each layer (YAML file) from the recipe manifest
        for layer in recipe_manifest.get("layers", []):
            layer_digest = layer.get("digest", "")
            layer_size = layer.get("size", 0)
            layer_annotations = layer.get("annotations", {})

            # Determine filename from annotations
            filename = layer_annotations.get("org.opencontainers.image.title", "")
            if not filename:
                # Use the recipe name annotation or digest
                recipe_name = layer_annotations.get("name", "")
                if recipe_name:
                    filename = f"{recipe_name}.yaml"
            # Annotations are registry-controlled: never let one escape layout_dir
            filename = _safe_layer_filename(filename, layer_digest)

            # Download the layer content
            layer_path = layout_dir / filename
            try:
                client.download_blob(recipe_url, layer_digest, str(layer_path))
                logger.info("Pulled %s (%d bytes)", filename, layer_size)
            except Exception as exc:
                logger.warning(
                    "Failed to download layer %s: %s", layer_digest[:16], exc
                )


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

    The oras Python SDK extracts layer files directly to layout_dir (flat structure),
    so we scan for YAML files there. Returns list of dicts with 'filename', 'content',
    'digest', 'size'.
    """
    extract_dir.mkdir(parents=True, exist_ok=True)
    extracted = []

    # Scan for YAML/YML files directly in layout_dir (flat structure from oras SDK)
    yaml_files = sorted(layout_dir.glob("*.yaml")) + sorted(layout_dir.glob("*.yml"))

    for yaml_path in yaml_files:
        try:
            content = yaml_path.read_text()
            digest = hashlib.sha256(content.encode()).hexdigest()
            size = len(content.encode())
            extracted.append(
                {
                    "filename": yaml_path.name,
                    "content": content,
                    "digest": f"sha256:{digest}",
                    "size": size,
                }
            )
        except Exception as exc:
            logger.warning("Failed to read recipe %s: %s", yaml_path.name, exc)

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
    # Before writing anything: a recipe already installed under a name from
    # before the slug rule is renamed to the name this install would give it,
    # so the install updates that file rather than leaving a second copy of
    # the same recipe under two ids.
    normalize_installed_recipe_names()
    installed = []

    for recipe in recipes:
        filename = _installed_filename(recipe["filename"])
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

        write_text_atomic(dest, recipe["content"])

        # Create metadata sidecar
        _write_recipe_meta(
            filename,
            reg["name"],
            name,
            version,
            recipe["digest"],
            display_name=_declared_name(recipe["content"]),
        )
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
    wanted = recipe_slug(recipe_name)
    target = None
    for r in recipes:
        # Match by file stem or by the ``name:`` the file declares, both
        # slugged. The browse drawer's Install button sends what the collection
        # *calls* the recipe, which is a display name; the artifact is named
        # for the file. Comparing raw stems matched only when a collection
        # happened to spell them the same way.
        if recipe_slug(r["filename"]) == wanted:
            target = r
            break
        if wanted and recipe_slug(_declared_name(r["content"])) == wanted:
            target = r
            break

    if not target:
        raise ValueError(
            f"Recipe '{recipe_name}' not found in collection '{collection_name}'"
        )

    # Check if already installed. The sweep first, for the same reason the
    # collection install runs it: update the file this recipe is already in.
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    normalize_installed_recipe_names()
    filename = _installed_filename(target["filename"])
    dest = RECIPES_DIR / filename
    action = "installed"

    if dest.exists() and not overwrite:
        try:
            with open(dest) as f:
                existing = f.read()
            if existing == target["content"]:
                return {
                    "success": True,
                    "recipe": recipe_name,
                    "recipe_id": installed_recipe_id(filename),
                    "action": "up_to_date",
                }
            else:
                action = "updated"
                logger.info(
                    "Local modifications detected for %s — overwriting",
                    filename,
                )
        except OSError:
            action = "updated"

    # Install/update the recipe
    write_text_atomic(dest, target["content"])

    # Update metadata
    _write_recipe_meta(
        filename,
        reg["name"],
        collection_name,
        version,
        target["digest"],
        display_name=_declared_name(target["content"]) or _recipe_stem(recipe_name),
    )

    logger.info(
        "%s recipe %s from %s:%s",
        action.capitalize(),
        recipe_name,
        collection_name,
        version,
    )
    # ``recipe`` is what the caller asked for; ``recipe_id`` is what it is now
    # called everywhere else — the id a deploy, a customization and an MCP tool
    # name it by. They differ whenever a collection's display name is not a
    # slug, which is the whole of this.
    return {
        "success": True,
        "recipe": recipe_name,
        "recipe_id": installed_recipe_id(filename),
        "action": action,
    }


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


def uninstall_oci_recipe(recipe_name: str) -> dict:
    """Uninstall an OCI-installed recipe.

    Removes the recipe YAML file and its .meta file.
    Returns dict with 'success', 'recipe', 'action' (uninstalled/not_found).
    """
    meta = get_oci_meta(recipe_name)
    if not meta:
        return {"success": False, "recipe": recipe_name, "action": "not_found"}

    recipe_file = _recipe_path(meta.name)
    meta_file = _meta_path(meta.name)

    # Remove files
    removed = []
    if recipe_file.exists():
        recipe_file.unlink()
        removed.append(str(recipe_file))
    if meta_file.exists():
        meta_file.unlink()
        removed.append(str(meta_file))

    logger.info(
        "Uninstalled OCI recipe %s (removed %d file(s))", recipe_name, len(removed)
    )
    return {
        "success": True,
        "recipe": recipe_name,
        "action": "uninstalled",
        "removed": removed,
    }


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
    #: The recipe's own ``name:`` — what a collection listing calls it, and
    #: what an id minted before the slug rule was made of. Recorded so a
    #: display name still finds the file it was installed as.
    display_name: str = ""
    #: Every stem this recipe has been on disk under, oldest first. Written by
    #: :func:`normalize_installed_recipe_names` when it renames one.
    previous_names: list[str] = field(default_factory=list)


# ── Recipe ids are slugs ─────────────────────────────────────────────────────

#: Everything a recipe file's stem — and so its ``oci-`` id — may carry.
#:
#: A collection names its recipes for people: ``Bonsai-2-27B (ternary,
#: llama.cpp)``. Installing that verbatim made the recipe id
#: ``oci-Bonsai-2-27B (ternary, llama.cpp)``, which then travelled in a URL
#: (``/api/recipes/customize/{id}``), in every deployment record that deployed
#: it, and through the MCP tools. The other two sources have never had this
#: problem because they are named by their file stem — ``bundled/qwen3.8-27b``,
#: ``custom-my-recipe`` — and this is that same rule, written down.
#:
#: The dot survives because recipe names carry version numbers and
#: ``qwen3.8-27b`` is a stem this project has always used: re-spelling it would
#: churn an id that was never the problem.
_SLUG_DISALLOWED = re.compile(r"[^a-z0-9._-]+")
_SLUG_RUNS = re.compile(r"-{2,}")


def recipe_slug(recipe_name: str) -> str:
    """The file stem an install writes, and so the id the recipe answers to.

    Lowercase; every run of anything outside ``a-z0-9._-`` becomes one dash.
    Idempotent — ``recipe_slug(recipe_slug(x)) == recipe_slug(x)`` — which is
    what lets the rename sweep below be run on every listing.
    """
    stem = _recipe_stem(recipe_name).lower()
    slug = _SLUG_RUNS.sub("-", _SLUG_DISALLOWED.sub("-", stem)).strip("-.")
    return slug


def _installed_filename(recipe_filename: str) -> str:
    """The name an install writes ``recipe_filename`` under, extension kept.

    A collection may ship ``.yml`` and that extension is preserved on disk, so
    only the stem is rewritten.
    """
    suffix = ".yml" if str(recipe_filename).endswith(".yml") else ".yaml"
    slug = recipe_slug(recipe_filename)
    return f"{slug or _recipe_stem(recipe_filename)}{suffix}"


def installed_recipe_id(recipe_filename: str) -> str:
    """The recipe id an installed file answers to.

    One definition, imported from the module that owns the prefix, so the id
    the OCI code reports and the id the listing mints cannot drift apart.
    """
    from spark_pulse.tools.recipe_sources import OCI_PREFIX

    return f"{OCI_PREFIX}{_recipe_stem(recipe_filename)}"


def _sidecar_stems() -> list[tuple[str, dict]]:
    """``(stem, raw sidecar)`` for every metadata file, cheaply.

    Read directly rather than through :func:`_read_recipe_meta`, which resolves
    a name through *this* — the scan is the fallback in that resolution, so it
    must not re-enter it.
    """
    if not RECIPES_DIR.is_dir():
        return []
    out: list[tuple[str, dict]] = []
    for meta_file in sorted(RECIPES_DIR.glob("*.meta")):
        try:
            with open(meta_file) as handle:
                data = yaml.safe_load(handle) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        out.append((_recipe_stem(meta_file.stem), data))
    return out


def _blocked_by(source: Path, target: Path) -> bool:
    """Is a *different* file already sitting at ``target``?

    Not just ``target.exists()``: on a case-insensitive filesystem that
    answers yes for the same file under another spelling, which is the very
    rename being asked for.
    """
    if not target.exists():
        return False
    try:
        return not source.samefile(target)
    except OSError:  # pragma: no cover - a path we cannot stat
        return True


def _installed_stems() -> dict[str, str]:
    """``{stem: suffix}`` for every recipe file in the directory.

    The directory is *listed* rather than probed with ``exists()`` because a
    case-insensitive filesystem answers yes to ``Gemma4-26B-A4B.yaml`` when
    what is there is ``gemma4-26b-a4b.yaml`` — and then the stem that came back
    would be the one asked for rather than the one on disk, which is the id.
    macOS is that filesystem; the Sparks are not. A rule that holds on one of
    them is not a rule.
    """
    if not RECIPES_DIR.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(RECIPES_DIR.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        out.setdefault(path.stem, path.suffix)
    return out


def _installed_stem(recipe_name: str) -> str:
    """The stem the named recipe actually has on disk.

    Four answers, in order, and the order is the point. The literal name wins
    when a file is there under it — a recipe installed before the slug rule
    that nothing has listed since is still that file. Then the slug, which is
    what every install writes. Then the sidecars, because a collection's
    display name and its artifact's file name are two different strings and
    only the sidecar knows they are one recipe (the Uninstall button in the
    browse drawer sends the first; the file is named the second). Failing all
    of that, the slug: a caller asking where a recipe *would* go gets the name
    it would be given.
    """
    stem = _recipe_stem(recipe_name)
    on_disk = _installed_stems()
    for candidate in (stem, recipe_slug(stem)):
        if candidate and candidate in on_disk:
            return candidate

    wanted = recipe_slug(stem)
    if wanted:
        for sidecar_stem, data in _sidecar_stems():
            names = [data.get("display_name", "")]
            names.extend(data.get("previous_names") or [])
            if any(name and recipe_slug(name) == wanted for name in names):
                return sidecar_stem
    return wanted or stem


def former_stems(recipe_name: str) -> list[str]:
    """Every stem the named recipe has been known by, newest name aside.

    The sidecar is the record: its ``display_name`` is what a collection calls
    the recipe and what an id minted before the slug rule was made of, and
    ``previous_names`` is what the rename sweep found on disk. An id built from
    either one is an id somebody's deployment record or customization may still
    be holding.
    """
    stem = _installed_stem(recipe_name)
    meta_file = RECIPES_DIR / f"{stem}.yaml.meta"
    if not meta_file.exists():
        return []
    try:
        with open(meta_file) as handle:
            data = yaml.safe_load(handle) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for name in [data.get("display_name", ""), *(data.get("previous_names") or [])]:
        name = str(name or "").strip()
        if name and name != stem and name not in out:
            out.append(name)
    return out


def normalize_installed_recipe_names() -> list[tuple[str, str]]:
    """Rename any installed recipe whose stem is not a slug. Idempotent.

    The one compatibility path this change keeps, and it is kept because the
    files are somebody's: a recipe installed before the slug rule sits in
    ``~/.config/spark-pulse/recipes`` under the name it was given, and the
    deployment records that deployed it name it that way too. Renaming it once,
    the first time anything lists the directory, is what makes *one* id true
    rather than two; :func:`spark_pulse.tools.recipe_sources.resolve_recipe`
    and :func:`spark_pulse.tools.custom_recipes.get_customized_recipe` are what
    keep the old one resolving afterwards.

    Nothing is ever overwritten: a stem whose slug is already taken by another
    file is left exactly where it is, because two recipes are not one recipe.

    Returns the ``(old id, new id)`` pairs it renamed, for the log and the
    tests; the sweep is otherwise silent and safe to call on every listing.
    """
    if not RECIPES_DIR.is_dir():
        return []

    on_disk = _installed_stems()
    renamed: list[tuple[str, str]] = []
    for stem, suffix in sorted(on_disk.items()):
        slug = recipe_slug(stem)
        if not slug or slug == stem:
            continue
        if slug in on_disk:
            logger.warning(
                "Not renaming %s%s to %s%s: a different recipe is already there",
                stem,
                suffix,
                slug,
                suffix,
            )
            continue
        path = RECIPES_DIR / f"{stem}{suffix}"
        target = RECIPES_DIR / f"{slug}{suffix}"
        try:
            path.rename(target)
        except OSError as exc:
            logger.warning("Could not rename %s to %s: %s", path.name, target.name, exc)
            continue
        on_disk[slug] = suffix

        old_meta = RECIPES_DIR / f"{stem}.yaml.meta"
        new_meta = RECIPES_DIR / f"{slug}.yaml.meta"
        if old_meta.exists() and not _blocked_by(old_meta, new_meta):
            try:
                old_meta.rename(new_meta)
            except OSError as exc:
                logger.warning("Could not rename %s: %s", old_meta.name, exc)
        _record_previous_name(new_meta, stem)
        logger.info("Renamed OCI recipe %s to %s", path.name, target.name)
        renamed.append((installed_recipe_id(stem), installed_recipe_id(slug)))
    return renamed


def _record_previous_name(meta_path: Path, stem: str) -> None:
    """Append ``stem`` to a sidecar's ``previous_names``, if there is a sidecar.

    A recipe with no sidecar — one an operator dropped into the directory by
    hand — keeps resolving through its own ``name:``, which is the other half
    of the alias and needs nothing written down.
    """
    if not meta_path.exists():
        return
    try:
        with open(meta_path) as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            return
        previous = [str(n) for n in (data.get("previous_names") or [])]
        if stem in previous:
            return
        previous.append(stem)
        data["previous_names"] = previous
        write_text_atomic(meta_path, yaml.dump(data, default_flow_style=False))
    except Exception as exc:  # pragma: no cover - a sidecar we cannot rewrite
        logger.debug("Could not record the previous name of %s: %s", stem, exc)


def _declared_name(content: str) -> str:
    """The ``name:`` a recipe file declares, or an empty string."""
    try:
        data = yaml.safe_load(content) or {}
    except Exception:
        return ""
    return str(data.get("name", "")).strip() if isinstance(data, dict) else ""


def _recipe_stem(recipe_name: str) -> str:
    """The recipe's name without its extension, whether or not it had one.

    ``Path.suffix`` cannot be used for this. Recipe names carry version
    numbers — ``GLM-4.7-Flash-AWQ``, ``Qwen3.8-27B`` — and Python reads
    ``.7-Flash-AWQ`` as an extension, so a name that came from a collection
    listing resolved to a metadata file that has never existed. That is why
    uninstalling from the browse drawer answered "not found" while the same
    recipe uninstalled fine from the installed list, which passes the name
    *with* ``.yaml``.
    """
    name = str(recipe_name).strip()
    for suffix in (".yaml", ".yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _meta_path(recipe_filename: str) -> Path:
    """Where the metadata for a recipe lives.

    Always ``<stem>.yaml.meta``, which is what every install has written —
    with the stem resolved by :func:`_installed_stem`, so a name that is not
    the one on disk (a display name, or one from before the slug rule) finds
    the sidecar that is.
    """
    return RECIPES_DIR / f"{_installed_stem(recipe_filename)}.yaml.meta"


def _recipe_path(recipe_name: str) -> Path:
    """The recipe file itself.

    ``.yaml`` is what an install writes, but a collection may ship ``.yml`` and
    that extension is kept on disk, so the one that exists wins. The ``.yaml``
    form is the answer when neither does — a caller asking where a recipe
    *would* go gets the name it would be given.
    """
    stem = _installed_stem(recipe_name)
    for suffix in (".yaml", ".yml"):
        candidate = RECIPES_DIR / f"{stem}{suffix}"
        if candidate.exists():
            return candidate
    return RECIPES_DIR / f"{stem}.yaml"


def _write_recipe_meta(
    recipe_filename: str,
    source: str,
    collection: str,
    version: str,
    digest: str,
    display_name: str = "",
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
        # What the collection calls this recipe, and every stem it has been on
        # disk under: the two halves of the alias that keeps an id minted
        # before the slug rule resolving. Carried forward rather than rebuilt,
        # because an update writes this file again and losing them here would
        # lose the only record that the rename happened.
        "display_name": display_name or existing.get("display_name", ""),
        "previous_names": list(existing.get("previous_names") or []),
    }

    meta_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(meta_path, yaml.dump(meta, default_flow_style=False))


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
    recipe_file = _recipe_path(recipe_filename)
    local_changes = False
    if recipe_file.exists():
        try:
            with open(recipe_file) as f:
                content = f.read()
            # Compare with the recorded digest. Stored digests are written as
            # "sha256:<hex>", so strip the algorithm prefix before comparing.
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            recorded = (data.get("digest") or "").split(":")[-1]
            if not recorded or content_hash != recorded:
                local_changes = True
        except OSError:
            pass

    return RecipeMeta(
        # The stem on disk, not the one asked for: a display name resolves to
        # the file it was installed as, and the installed list must name the
        # file, because that name is the recipe's id.
        name=f"{_installed_stem(recipe_filename)}.yaml",
        source=data.get("source", ""),
        collection=data.get("collection", ""),
        version=data.get("version", ""),
        digest=data.get("digest", ""),
        installed_at=data.get("installed_at", ""),
        updated_at=data.get("updated_at", ""),
        local_changes=local_changes,
        display_name=str(data.get("display_name", "") or ""),
        previous_names=[str(n) for n in (data.get("previous_names") or [])],
    )


def list_oci_recipes() -> list[RecipeMeta]:
    """List all recipes that were installed from OCI collections."""
    normalize_installed_recipe_names()
    if not RECIPES_DIR.is_dir():
        return []

    result = []
    for meta_file in sorted(RECIPES_DIR.glob("*.meta")):
        # Derive the recipe filename from the meta filename
        base = meta_file.stem  # e.g. "qwen3-8b.yaml"
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
