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

from spark_pulse.mock.oci_registry import (
    mock_check_updates,
    mock_install_collection,
    mock_list_collection_recipes,
    mock_list_collections,
    mock_list_oci_recipes,
)


class TestCollections:
    def test_the_catalogue_is_listed(self):
        collections = mock_list_collections()
        assert [c.name for c in collections] == ["spark-recipes", "community-recipes"]
        assert collections[0].version == "1.0.0"

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
        ]
        assert recipes[0].solo_only is True
        assert recipes[2].cluster_only is True

    def test_an_unknown_collection_holds_nothing(self):
        assert mock_list_collection_recipes("ghost") == []


class TestInstall:
    def test_an_install_answers_with_the_files_it_would_write(self):
        """And writes none of them: simulation shares the operator's own
        ``~/.config/spark-pulse/recipes``, so a pretend install that left real
        files behind would be indistinguishable from one they asked for."""
        installed = mock_install_collection(name="spark-recipes", version="1.0.0")

        assert installed == [
            "qwen3-8b.yaml",
            "llama-3-8b.yaml",
            "llama-3-70b.yaml",
            "mistral-22b.yaml",
            "mixtral-8x7b.yaml",
        ]

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
        assert [r.name for r in recipes] == ["qwen3-8b.yaml", "llama-3-8b.yaml"]
        assert all(r.collection == "spark-recipes" for r in recipes)
