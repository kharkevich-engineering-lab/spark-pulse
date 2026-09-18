"""Simulated OCI registry — the whole of it, in one place.

Every name here stands in for one in :mod:`spark_pulse.tools.oci_registry` and
answers with the same shape, so ``routers/oci.py`` picks the function and then
serialises the result exactly once, whichever mode it is in. It used to be two
implementations: this module, which nothing but its own test called, and a
near-identical copy of the same canned catalogue living at the bottom of the
router, which is what simulation actually ran. A mock inside a real router is
the thing `CLAUDE.md` puts in ``mock/``, and a second copy of the data is how
the two drifted — the router's collections carried no ``display_version`` and
its recipes no ``solo_only``, so a simulated browse showed a page the real one
never would.

Nothing here touches the disk. An install answers with the filenames it would
have written: simulation shares the operator's real
``~/.config/spark-pulse/recipes``, and a pretend install that left real files
behind would be indistinguishable from one they asked for.
"""

from __future__ import annotations

from spark_pulse.tools.oci_registry import (
    CollectionInfo,
    CollectionRecipe,
    RecipeMeta,
    UpdateInfo,
)

# ── The canned catalogue ─────────────────────────────────────────────────────

#: The collections a simulated registry offers. One copy: the listing, the
#: recipe browse and the install validation all read it.
_COLLECTIONS: list[CollectionInfo] = [
    CollectionInfo(
        name="spark-recipes",
        version="1.0.0",
        description="Spark Pulse recipe collection",
        vendor="Kharkevich Engineering Lab",
        license="MIT",
        recipe_count=5,
        digest="sha256:abc123def456",
        registry="ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
    ),
    CollectionInfo(
        name="community-recipes",
        version="0.3.0",
        description="Community-contributed recipes",
        vendor="Community",
        license="Apache-2.0",
        recipe_count=3,
        digest="sha256:789ghi012jkl",
        registry="ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
    ),
]

#: What each collection holds. The names are models, because that is what a
#: recipe is named after.
_COLLECTION_RECIPES: dict[str, list[CollectionRecipe]] = {
    "spark-recipes": [
        CollectionRecipe(
            name="qwen3-8b",
            description="Qwen3 8B inference with vLLM",
            model="Qwen/Qwen3-8B",
            container="vllm-node",
            recipe_version="1.0.0",
            solo_only=True,
        ),
        CollectionRecipe(
            name="llama-3-8b",
            description="Llama 3.1 8B inference with vLLM",
            model="meta-llama/Llama-3.1-8B-Instruct",
            container="vllm-node",
            recipe_version="1.0.0",
        ),
        CollectionRecipe(
            name="llama-3-70b",
            description="Llama 3.1 70B inference with vLLM",
            model="meta-llama/Llama-3.1-70B-Instruct",
            container="vllm-node",
            recipe_version="1.0.0",
            cluster_only=True,
        ),
        CollectionRecipe(
            name="mistral-22b",
            description="Mistral 22B inference with vLLM",
            model="mistralai/Mistral-22B-Instruct-v0.1",
            container="vllm-node",
            recipe_version="1.0.0",
        ),
        CollectionRecipe(
            name="mixtral-8x7b",
            description="Mixtral 8x7B inference with vLLM",
            model="mistralai/Mixtral-8x7B-Instruct-v0.1",
            container="vllm-node",
            recipe_version="1.0.0",
        ),
    ],
    "community-recipes": [
        CollectionRecipe(
            name="community-llama-3-8b",
            description="Community-tuned Llama 3 8B",
            model="meta-llama/Llama-3-8B",
            container="vllm-node",
            recipe_version="0.3.0",
        ),
        CollectionRecipe(
            name="community-mixtral-8x7b",
            description="Community-tuned Mixtral 8x7B",
            model="mistralai/Mixtral-8x7B-Instruct-v0.1",
            container="vllm-node",
            recipe_version="0.3.0",
        ),
        CollectionRecipe(
            name="community-qwen-72b",
            description="Qwen 2.5 72B inference",
            model="Qwen/Qwen2.5-72B-Instruct",
            container="vllm-node",
            recipe_version="0.3.0",
        ),
    ],
}


# ── Registries ───────────────────────────────────────────────────────────────

#: Mutable for the life of the process: the registry endpoints are CRUD, and a
#: simulated one that forgot every write would be a worse rehearsal than none.
_REGISTRY_STATE: list[dict] = [
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


def mock_list_registries() -> list[dict]:
    """Every configured registry, as ``list_registries`` reports them."""
    return list(_REGISTRY_STATE)


def mock_add_registry(registry: dict) -> dict:
    """Add a registry, replacing any of the same name."""
    name = registry.get("name", "")
    _REGISTRY_STATE[:] = [r for r in _REGISTRY_STATE if r["name"] != name]
    added = {
        "name": name,
        "url": registry.get("url", ""),
        "enabled": registry.get("enabled", True),
        "default": registry.get("default", False),
        "auth_type": registry.get("auth_type", "none"),
        "connected": False,
    }
    _REGISTRY_STATE.append(added)
    return added


def mock_update_registry(name: str, updates: dict) -> dict | None:
    """Merge ``updates`` into one registry, or ``None`` when there is none."""
    for i, registry in enumerate(_REGISTRY_STATE):
        if registry["name"] == name:
            _REGISTRY_STATE[i].update(updates)
            return _REGISTRY_STATE[i]
    return None


def mock_remove_registry(name: str) -> bool:
    """Remove a registry. ``True`` when one was found and removed."""
    before = len(_REGISTRY_STATE)
    _REGISTRY_STATE[:] = [r for r in _REGISTRY_STATE if r["name"] != name]
    return len(_REGISTRY_STATE) < before


def mock_test_registry_connection(name: str) -> bool:
    """A simulated registry always answers."""
    return True


def mock_list_tags(name: str) -> list[str]:
    """The version tags a simulated registry offers."""
    return ["1.0.0", "1.0.1", "latest"]


# ── Collections ──────────────────────────────────────────────────────────────


def mock_list_collections(
    registry_name: str | None = None, version: str | None = None
) -> list[CollectionInfo]:
    """The canned collections, filtered the way the real lister filters."""
    collections = list(_COLLECTIONS)
    if registry_name:
        collections = [c for c in collections if c.registry == registry_name]
    if version:
        collections = [c for c in collections if c.version == version]
    return collections


def mock_list_collection_recipes(
    collection_name: str,
    registry_name: str | None = None,
    version: str | None = None,
) -> list[CollectionRecipe]:
    """The recipes in one canned collection; empty for a name nobody offers."""
    return list(_COLLECTION_RECIPES.get(collection_name, []))


def mock_install_collection(
    name: str,
    version: str,
    registry_name: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """The filenames installing this collection would write.

    Raises ``ValueError`` for a collection or version nobody offers, which is
    what the real installer raises and what the router turns into a 404.
    """
    if not any(c.name == name and c.version == version for c in _COLLECTIONS):
        raise ValueError(f"Collection '{name}:{version}' not found")
    recipes = _COLLECTION_RECIPES.get(name, [])
    return [f"{r.name}.yaml" for r in recipes] or [f"{name}.yaml"]


# ── Single recipes ───────────────────────────────────────────────────────────

#: Which recipes this process has been asked to install, so a second install
#: can answer "up to date" and an update can answer at all.
_INSTALLED_RECIPES: set[str] = set()


def mock_install_oci_recipe(
    collection_name: str,
    recipe_name: str,
    version: str = "",
    registry_name: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Install one recipe; installing it twice is a no-op, not an error."""
    key = f"{collection_name}/{recipe_name}"
    if key in _INSTALLED_RECIPES:
        return {"success": True, "recipe": recipe_name, "action": "up_to_date"}
    _INSTALLED_RECIPES.add(key)
    return {"success": True, "recipe": recipe_name, "action": "installed"}


def mock_update_oci_recipe(
    recipe_name: str,
    collection_name: str,
    version: str | None = None,
    registry_name: str | None = None,
) -> dict:
    """Update one installed recipe. ``ValueError`` when it was never installed."""
    if f"{collection_name}/{recipe_name}" not in _INSTALLED_RECIPES:
        raise ValueError(f"Recipe '{recipe_name}' is not installed")
    return {"success": True, "recipe": recipe_name, "action": "updated"}


# ── Updates and metadata ─────────────────────────────────────────────────────


def mock_check_updates(
    collection: str | None = None, registry: str | None = None
) -> list[UpdateInfo]:
    """One collection with an update waiting."""
    return [
        UpdateInfo(
            collection="spark-recipes",
            current_version="1.0.0",
            latest_version="1.1.0",
            current_digest="sha256:abc123def456",
            latest_digest="sha256:new789xyz",
            local_changes=False,
            added_recipes=["mixtral-8x7b.yaml"],
            modified_recipes=["qwen3-8b.yaml"],
        ),
    ]


def mock_list_oci_recipes() -> list[RecipeMeta]:
    """The OCI-installed recipes a simulated control plane already holds."""
    return [
        RecipeMeta(
            name="qwen3-8b.yaml",
            source="spark-official",
            collection="spark-recipes",
            version="1.0.0",
            digest="sha256:abc123",
            installed_at="2026-06-15T02:00:00Z",
            updated_at="2026-06-15T02:00:00Z",
            local_changes=False,
        ),
        RecipeMeta(
            name="llama-3-8b.yaml",
            source="spark-official",
            collection="spark-recipes",
            version="1.0.0",
            digest="sha256:def456",
            installed_at="2026-06-15T02:00:00Z",
            updated_at="2026-06-15T02:00:00Z",
            local_changes=False,
        ),
    ]
