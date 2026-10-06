"""The simulated OCI registry answers in the real one's shapes.

This module used to be a second, unreachable catalogue: the router carried its
own copy of the same canned data and that is what simulation ran, so nothing
here was ever exercised by a request. It is now the one implementation, which
makes these tests the contract the router relies on — a collection is a
``CollectionInfo``, a recipe a ``CollectionRecipe``, an update an
``UpdateInfo``, because the router serialises the real objects and the
simulated ones through the same code.
"""

import pytest

from spark_pulse.mock import oci_registry as simulated
from spark_pulse.mock.oci_registry import (
    mock_apply_collection_recipes,
    mock_check_updates,
    mock_collection_state,
    mock_install_collection,
    mock_install_oci_recipe,
    mock_list_collection_recipes,
    mock_list_collections,
    mock_list_oci_recipes,
)


@pytest.fixture(autouse=True)
def _fresh_install_state():
    """The simulated install state is process-wide; every test starts canned."""
    simulated.reset_installed()
    yield
    simulated.reset_installed()


class TestCollections:
    def test_the_catalogue_is_listed(self):
        collections = mock_list_collections()
        assert [c.name for c in collections] == ["spark-recipes", "community-recipes"]
        assert collections[0].version == "1.1.0"

    def test_a_registry_nobody_offers_matches_nothing(self):
        assert mock_list_collections(registry_name="spark-official") == []

    def test_a_version_filter_narrows_the_listing(self):
        collections = mock_list_collections(version="0.3.0")
        assert [c.name for c in collections] == ["community-recipes"]

    def test_a_collection_lists_recipes_shaped_like_the_real_ones(self):
        recipes = mock_list_collection_recipes("spark-recipes")
        assert [r.name for r in recipes] == [
            "qwen3-8b",
            "llama-3-8b",
            "llama-3-70b",
            "mistral-22b",
            "mixtral-8x7b",
            # A collection names its recipes for people, and one of these does.
            "Bonsai-2-27B (ternary, llama.cpp)",
            "Qwen3-Embedding-4B",
        ]
        assert recipes[-1].serves == "embedding"
        assert recipes[0].solo_only is True
        assert recipes[2].cluster_only is True

    def test_an_unknown_collection_holds_nothing(self):
        assert mock_list_collection_recipes("ghost") == []


class TestInstall:
    def test_an_install_answers_with_the_files_it_would_write(self):
        """And writes none of them: simulation shares the operator's own
        ``~/.config/spark-pulse/recipes``, so a pretend install that left real
        files behind would be indistinguishable from one they asked for.

        The last one is the point: a display name is written as its slug, here
        as in the real installer, because the file stem is the recipe's id.
        """
        installed = mock_install_collection(name="spark-recipes", version="1.1.0")

        assert installed == [
            "qwen3-8b.yaml",
            "llama-3-8b.yaml",
            "llama-3-70b.yaml",
            "mistral-22b.yaml",
            "mixtral-8x7b.yaml",
            "bonsai-2-27b-ternary-llama.cpp.yaml",
            "qwen3-embedding-4b.yaml",
        ]

    def test_installing_one_recipe_answers_with_its_id(self):
        """The name asked for and the id it got, kept apart."""
        result = mock_install_oci_recipe(
            collection_name="spark-recipes",
            recipe_name="Bonsai-2-27B (ternary, llama.cpp)",
        )

        assert result["recipe"] == "Bonsai-2-27B (ternary, llama.cpp)"
        assert result["recipe_id"] == "oci-bonsai-2-27b-ternary-llama.cpp"

    def test_a_version_nobody_offers_is_refused(self):
        """``ValueError`` is what the real installer raises, and it is what the
        router turns into a 404 — the same branch for both modes."""
        with pytest.raises(ValueError, match="spark-recipes:9.9.9"):
            mock_install_collection(name="spark-recipes", version="9.9.9")


class TestUpdatesAndMetadata:
    def test_one_collection_has_an_update_waiting(self):
        updates = mock_check_updates()
        assert [u.collection for u in updates] == ["spark-recipes"]
        assert updates[0].current_version == "1.0.0"
        assert updates[0].latest_version == "1.1.0"

    def test_the_installed_recipes_carry_their_provenance(self):
        recipes = mock_list_oci_recipes()
        assert [r.name for r in recipes] == [
            "bonsai-2-27b-ternary-llama.cpp.yaml",
            "gemma-2-9b.yaml",
            "llama-3-8b.yaml",
            "qwen3-8b.yaml",
        ]
        assert all(r.collection == "spark-recipes" for r in recipes)


class TestCollectionState:
    """Simulation must show every state, or the view can only be rehearsed in
    part: the e2e spec and the screenshots both read this."""

    def _states(self) -> dict[str, str]:
        state = mock_collection_state("spark-recipes")
        return {r["name"]: r["state"] for r in state["recipes"]}

    def test_every_state_is_shown(self):
        assert self._states() == {
            "qwen3-8b": "installed",
            "llama-3-8b": "update",
            "llama-3-70b": "not_installed",
            "mistral-22b": "not_installed",
            "mixtral-8x7b": "not_installed",
            "Bonsai-2-27B (ternary, llama.cpp)": "local_edits",
            "Qwen3-Embedding-4B": "not_installed",
            "gemma-2-9b": "removed",
        }

    def test_the_header_names_the_version_furthest_behind(self):
        state = mock_collection_state("spark-recipes")
        assert (state["installed_version"], state["latest_version"]) == (
            "1.0.0",
            "1.1.0",
        )

    def test_an_unknown_collection_is_refused(self):
        with pytest.raises(ValueError):
            mock_collection_state("ghost")
        with pytest.raises(ValueError):
            mock_collection_state("spark-recipes", registry_name="elsewhere")

    def test_apply_installs_updates_and_skips_local_edits(self):
        result = mock_apply_collection_recipes(
            "spark-recipes",
            ["llama-3-70b", "llama-3-8b", "Bonsai-2-27B (ternary, llama.cpp)", "nope"],
        )
        actions = {
            r["recipe"]: (r["success"], r.get("action")) for r in result["results"]
        }
        assert actions == {
            "llama-3-70b": (True, "installed"),
            "llama-3-8b": (True, "updated"),
            "Bonsai-2-27B (ternary, llama.cpp)": (True, "skipped_local_edits"),
            "nope": (False, None),
        }
        states = self._states()
        assert states["llama-3-70b"] == "installed"
        assert states["llama-3-8b"] == "installed"
        assert states["Bonsai-2-27B (ternary, llama.cpp)"] == "local_edits"

    def test_overwrite_replaces_local_edits_and_current_is_up_to_date(self):
        result = mock_apply_collection_recipes(
            "spark-recipes",
            ["Bonsai-2-27B (ternary, llama.cpp)", "qwen3-8b"],
            overwrite_local=True,
        )
        assert [r["action"] for r in result["results"]] == ["updated", "up_to_date"]
        assert self._states()["Bonsai-2-27B (ternary, llama.cpp)"] == "installed"

    def test_apply_to_an_unknown_collection_is_refused(self):
        with pytest.raises(ValueError):
            mock_apply_collection_recipes("ghost", ["x"])
