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
    _version_sort_key,
    installed_recipe_id,
    recipe_slug,
    recipe_states,
)

# ── The canned catalogue ─────────────────────────────────────────────────────

#: The collections a simulated registry offers. One copy: the listing, the
#: recipe browse and the install validation all read it.
_COLLECTIONS: list[CollectionInfo] = [
    CollectionInfo(
        name="spark-recipes",
        # Ahead of what is installed below, so the collection view has an
        # update to show: ``1.0.0 → 1.1.0``, as ``mock_check_updates`` says.
        version="1.1.0",
        description="Spark Pulse recipe collection",
        vendor="Kharkevich Engineering Lab",
        license="MIT",
        recipe_count=7,
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
        # Named the way a collection really names a recipe — for a person, with
        # spaces, parentheses and a comma in it. An install writes the *slug*
        # of this and never this, because the file stem is the recipe's id and
        # an id travels in a URL, in a deployment record and through the MCP
        # tools. Simulation carries one so the rule can be seen at all.
        CollectionRecipe(
            name="Bonsai-2-27B (ternary, llama.cpp)",
            description="Bonsai 2 27B, ternary weights, on llama.cpp",
            model="deepgrove/Bonsai-2-27B",
            container="llama-cpp-node",
            recipe_version="1.0.0",
        ),
        # Not chat: the collection view marks it, as the recipe card does.
        CollectionRecipe(
            name="Qwen3-Embedding-4B",
            description="Qwen3 embeddings at 4B, pooling runner",
            model="Qwen/Qwen3-Embedding-4B",
            container="vllm-node",
            recipe_version="1.1.0",
            serves="embedding",
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
    return ["1.1.0", "1.0.0", "latest"]


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
    # The slug, which is what the real installer writes: these filenames are
    # the stems the recipes will be listed under, and so their ids.
    return [f"{recipe_slug(r.name)}.yaml" for r in recipes] or [
        f"{recipe_slug(name)}.yaml"
    ]


# ── What is installed ────────────────────────────────────────────────────────

#: The content digest of each recipe in the newest version of its collection —
#: what the real collection view reads off the pulled files. A recipe whose
#: installed digest differs from its entry here has an update waiting.
_LATEST_DIGESTS: dict[str, dict[str, str]] = {
    collection: {r.name: f"sha256:{recipe_slug(r.name)}-latest" for r in recipes}
    for collection, recipes in _COLLECTION_RECIPES.items()
}


def _initial_installed() -> dict[str, RecipeMeta]:
    """What a simulated control plane holds before anybody touches it.

    One of every state the collection view can show, so a simulated Library is
    a rehearsal of all of them: one installed and current, one with an update
    waiting, one the operator edited (with an update behind it, so the
    overwrite question is asked), and one the collection no longer ships.
    """
    latest = _LATEST_DIGESTS["spark-recipes"]

    def meta(stem: str, version: str, digest: str, **extra) -> RecipeMeta:
        return RecipeMeta(
            name=f"{stem}.yaml",
            source="ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
            collection="spark-recipes",
            version=version,
            digest=digest,
            installed_at="2026-06-15T02:00:00Z",
            updated_at="2026-06-15T02:00:00Z",
            local_changes=extra.pop("local_changes", False),
            **extra,
        )

    return {
        "qwen3-8b": meta("qwen3-8b", "1.1.0", latest["qwen3-8b"]),
        "llama-3-8b": meta("llama-3-8b", "1.0.0", "sha256:llama-3-8b-1.0.0"),
        "bonsai-2-27b-ternary-llama.cpp": meta(
            "bonsai-2-27b-ternary-llama.cpp",
            "1.0.0",
            "sha256:bonsai-1.0.0",
            local_changes=True,
            display_name="Bonsai-2-27B (ternary, llama.cpp)",
            previous_names=["Bonsai-2-27B (ternary, llama.cpp)"],
        ),
        "gemma-2-9b": meta(
            "gemma-2-9b", "1.0.0", "sha256:gemma-2-9b-1.0.0", display_name="gemma-2-9b"
        ),
    }


#: Mutable for the life of the process, keyed by stem: an install here is what
#: the next state read reports, and nothing is written to disk.
_INSTALLED: dict[str, RecipeMeta] = _initial_installed()


def reset_installed() -> None:
    """Back to the canned starting point. For tests, which share the process."""
    _INSTALLED.clear()
    _INSTALLED.update(_initial_installed())


def _install(collection_name: str, recipe_name: str) -> str:
    """Record one recipe as installed at the latest content. Returns its stem."""
    stem = recipe_slug(recipe_name)
    version = next(
        (c.version for c in _COLLECTIONS if c.name == collection_name), "1.0.0"
    )
    _INSTALLED[stem] = RecipeMeta(
        name=f"{stem}.yaml",
        source=next(
            (c.registry for c in _COLLECTIONS if c.name == collection_name), ""
        ),
        collection=collection_name,
        version=version,
        digest=_LATEST_DIGESTS.get(collection_name, {}).get(recipe_name, ""),
        installed_at="2026-06-15T02:00:00Z",
        updated_at="2026-06-15T02:00:00Z",
        local_changes=False,
        display_name=recipe_name,
    )
    return stem


# ── Single recipes ───────────────────────────────────────────────────────────


def mock_install_oci_recipe(
    collection_name: str,
    recipe_name: str,
    version: str = "",
    registry_name: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Install one recipe; installing it twice is a no-op, not an error.

    Keyed by the slug, because that is the file the real install writes and two
    spellings of one display name are one recipe.
    """
    recipe_id = installed_recipe_id(recipe_slug(recipe_name))
    current = _INSTALLED.get(recipe_slug(recipe_name))
    latest = _LATEST_DIGESTS.get(collection_name, {}).get(recipe_name)
    if current and current.digest == latest and not current.local_changes:
        return {
            "success": True,
            "recipe": recipe_name,
            "recipe_id": recipe_id,
            "action": "up_to_date",
        }
    _install(collection_name, recipe_name)
    return {
        "success": True,
        "recipe": recipe_name,
        "recipe_id": recipe_id,
        "action": "updated" if current else "installed",
    }


def mock_update_oci_recipe(
    recipe_name: str,
    collection_name: str,
    version: str | None = None,
    registry_name: str | None = None,
) -> dict:
    """Update one installed recipe. ``ValueError`` when it was never installed."""
    if recipe_slug(recipe_name) not in _INSTALLED:
        raise ValueError(f"Recipe '{recipe_name}' is not installed")
    _install(collection_name, recipe_name)
    return {
        "success": True,
        "recipe": recipe_name,
        "recipe_id": installed_recipe_id(recipe_slug(recipe_name)),
        "action": "updated",
    }


# ── The collection view ──────────────────────────────────────────────────────


def mock_collection_state(name: str, registry_name: str | None = None) -> dict:
    """The collection view, derived by the real rule from the canned state.

    Only the inputs are simulated: which recipes are listed, what the newest
    version's digests are, and what is installed. Matching and the states
    themselves are :func:`recipe_states`, the code the real view runs.
    """
    collection = next((c for c in _COLLECTIONS if c.name == name), None)
    if collection is None or (registry_name and collection.registry != registry_name):
        raise ValueError(f"Collection '{name}' not found")
    metas = [m for m in _INSTALLED.values() if m.collection == name]
    versions = sorted({m.version for m in metas}, key=_version_sort_key)
    return {
        "collection": name,
        "registry": collection.registry,
        "description": collection.description,
        "latest_version": collection.version,
        "display_version": collection.display_version or collection.version,
        "installed_version": versions[0] if versions else "",
        "checked": True,
        "recipes": recipe_states(
            _COLLECTION_RECIPES.get(name, []), metas, _LATEST_DIGESTS.get(name, {})
        ),
    }


def mock_apply_collection_recipes(
    collection_name: str,
    recipe_names: list[str],
    version: str | None = None,
    registry_name: str | None = None,
    overwrite_local: bool = False,
) -> dict:
    """Install or update several recipes, one result each, as the real one does.

    A recipe edited locally is skipped unless ``overwrite_local``; a name the
    collection does not carry fails on its own without stopping the rest.
    """
    collection = next((c for c in _COLLECTIONS if c.name == collection_name), None)
    if collection is None:
        raise ValueError(f"Collection '{collection_name}' not found")
    latest = _LATEST_DIGESTS.get(collection_name, {})
    results: list[dict] = []
    for recipe_name in recipe_names:
        stem = recipe_slug(recipe_name)
        result: dict = {
            "recipe": recipe_name,
            "recipe_id": installed_recipe_id(stem),
            "success": False,
        }
        current = _INSTALLED.get(stem)
        if recipe_name not in latest:
            result["error"] = (
                f"Recipe '{recipe_name}' is not in "
                f"{collection_name}:{collection.version}"
            )
        elif current and current.digest == latest[recipe_name]:
            result.update(success=True, action="up_to_date")
        elif current and current.local_changes and not overwrite_local:
            result.update(success=True, action="skipped_local_edits")
        else:
            _install(collection_name, recipe_name)
            result.update(success=True, action="updated" if current else "installed")
        results.append(result)
    return {
        "collection": collection_name,
        "version": version or collection.version,
        "results": results,
    }


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
    """The OCI-installed recipes a simulated control plane holds now."""
    return [_INSTALLED[stem] for stem in sorted(_INSTALLED)]
