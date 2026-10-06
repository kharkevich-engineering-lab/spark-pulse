"""A collection's recipes against what is installed: matching, state, apply.

The collection view offered Install on every recipe, installed or not, because
it decided "installed" in the browser by comparing the collection's display
name to the installed file's stem — and since the slug rule those are two
different strings. Matching is now one backend function, and these are its
contract: identity by slug, by the sidecar's ``display_name`` or by a stem it
used to have, never by resemblance.
"""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

import spark_pulse.tools.oci_registry as oci
from spark_pulse.tools.oci_registry import CollectionInfo, CollectionRecipe, RecipeMeta


def _meta(stem: str, digest: str = "sha256:old", **extra) -> RecipeMeta:
    return RecipeMeta(
        name=f"{stem}.yaml",
        source="ghcr",
        collection="spark-recipes",
        version=extra.pop("version", "1.0.0"),
        digest=digest,
        installed_at="",
        updated_at="",
        local_changes=extra.pop("local_changes", False),
        **extra,
    )


def _listed(name: str, **extra) -> CollectionRecipe:
    return CollectionRecipe(
        name=name,
        description="",
        model="",
        container="",
        recipe_version="",
        **extra,
    )


def _digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


# ── Matching ─────────────────────────────────────────────────────────────────


class TestMatchInstalled:
    def test_a_display_name_matches_the_slug_it_was_installed_as(self):
        """The reported bug: the listing says one thing, the file another."""
        meta = _meta("qwen3.5-397b-int4-autoround-pp-3")
        matched = oci.match_installed(["Qwen3.5-397B-INT4-Autoround (PP=3)"], [meta])
        assert matched == {"Qwen3.5-397B-INT4-Autoround (PP=3)": meta}

    def test_the_sidecar_display_name_matches_when_the_slug_does_not(self):
        """A file whose stem came from its layer title, not from its name."""
        meta = _meta("bonsai", display_name="Bonsai-2-27B (ternary)")
        assert oci.match_installed(["Bonsai-2-27B (ternary)"], [meta]) == {
            "Bonsai-2-27B (ternary)": meta
        }

    def test_a_former_stem_matches(self):
        meta = _meta("renamed", previous_names=["Old Name"])
        assert oci.match_installed(["Old Name"], [meta]) == {"Old Name": meta}

    def test_the_file_the_collection_ships_it_in_matches(self):
        meta = _meta("from-the-title")
        assert oci.match_installed(
            ["Display"], [meta], file_stems={"Display": "from-the-title"}
        ) == {"Display": meta}

    def test_slugs_that_differ_only_by_punctuation_are_two_recipes(self):
        """``qwen3.5-x`` and ``qwen3-5-x`` look alike and are not one recipe."""
        meta = _meta("qwen3.5-x")
        assert oci.match_installed(["Qwen3-5-X", "Qwen3 5 X"], [meta]) == {}

    def test_an_installed_recipe_is_claimed_once_and_by_the_strongest_match(self):
        """The slug match wins over a display-name match for another listing."""
        meta = _meta("alpha", display_name="Beta")
        assert oci.match_installed(["Beta", "Alpha"], [meta]) == {"Alpha": meta}

    def test_nothing_installed_matches_nothing(self):
        assert oci.match_installed(["a", "b"], []) == {}


# ── States ───────────────────────────────────────────────────────────────────


class TestRecipeStates:
    def _states(self, listed, metas, latest):
        return {r["name"]: r["state"] for r in oci.recipe_states(listed, metas, latest)}

    def test_each_listed_recipe_has_exactly_one_state(self):
        listed = [_listed(n) for n in ("current", "behind", "edited", "absent")]
        metas = [
            _meta("current", "sha256:new"),
            _meta("behind", "sha256:old"),
            _meta("edited", "sha256:old", local_changes=True),
        ]
        latest = {n: "sha256:new" for n in ("current", "behind", "edited", "absent")}
        assert self._states(listed, metas, latest) == {
            "current": "installed",
            "behind": "update",
            "edited": "local_edits",
            "absent": "not_installed",
        }

    def test_local_edits_still_say_whether_upstream_changed(self):
        listed = [_listed("edited"), _listed("edited-current")]
        metas = [
            _meta("edited", "sha256:old", local_changes=True),
            _meta("edited-current", "sha256:new", local_changes=True),
        ]
        rows = oci.recipe_states(
            listed, metas, {"edited": "sha256:new", "edited-current": "sha256:new"}
        )
        assert [(r["state"], r["update_available"]) for r in rows] == [
            ("local_edits", True),
            ("local_edits", False),
        ]

    def test_an_installed_recipe_no_longer_listed_is_removed_upstream(self):
        metas = [_meta("gone", display_name="Gone (v1)", version="0.9.0")]
        rows = oci.recipe_states([_listed("kept")], metas, {"kept": "sha256:x"})
        assert rows[-1] == {
            "name": "Gone (v1)",
            "recipe_id": "oci-gone",
            "description": "",
            "model": "",
            "container": "",
            "solo_only": False,
            "cluster_only": False,
            "serves": "chat",
            "state": "removed",
            "installed_version": "0.9.0",
            "update_available": False,
            "local_changes": False,
        }

    def test_unknown_upstream_content_is_never_called_an_update(self):
        """The newest version could not be read: unknown is not changed."""
        listed = [_listed("behind")]
        assert self._states(listed, [_meta("behind")], None) == {"behind": "installed"}

    def test_the_id_is_the_installed_stem_or_the_one_an_install_would_write(self):
        listed = [_listed("Shown Name", serves="embedding"), _listed("New One (x)")]
        metas = [_meta("on-disk", display_name="Shown Name")]
        rows = oci.recipe_states(listed, metas, {})
        assert [r["recipe_id"] for r in rows] == ["oci-on-disk", "oci-new-one-x"]
        assert rows[0]["serves"] == "embedding"


# ── The view, end to end with the registry patched out ───────────────────────


@pytest.fixture
def recipes_dir(tmp_path, monkeypatch):
    path = tmp_path / "recipes"
    path.mkdir()
    monkeypatch.setattr(oci, "RECIPES_DIR", path)
    monkeypatch.setattr(oci, "OCI_CACHE_DIR", tmp_path / "oci-cache")
    return path


def _collection(version: str) -> CollectionInfo:
    return CollectionInfo(
        name="spark-recipes",
        version=version,
        description="d",
        vendor="",
        license="",
        recipe_count=2,
        digest="",
        registry="ghcr",
        display_version=f"v{version}",
    )


REG = {"name": "ghcr", "url": "ghcr.io/acme/recipes"}

ALPHA_V1 = "name: Alpha (one)\nmodel: a\n"
ALPHA_V2 = "name: Alpha (one)\nmodel: a2\n"
BETA = "name: beta\nmodel: b\n"


def _pulled(*files: tuple[str, str]) -> list[dict]:
    return [
        {"filename": name, "content": content, "digest": _digest(content)}
        for name, content in files
    ]


def _install(recipes_dir, filename: str, content: str, **meta_extra) -> None:
    (recipes_dir / filename).write_text(content)
    oci._write_recipe_meta(
        filename,
        "ghcr",
        meta_extra.pop("collection", "spark-recipes"),
        meta_extra.pop("version", "1.0.0"),
        _digest(content),
        display_name=meta_extra.pop("display_name", ""),
    )


class TestCollectionState:
    def test_the_newest_version_is_compared_by_content(self, recipes_dir):
        _install(recipes_dir, "alpha-one.yaml", ALPHA_V1, display_name="Alpha (one)")
        _install(recipes_dir, "other.yaml", BETA, collection="another-collection")
        with (
            patch.object(
                oci,
                "list_collections",
                return_value=[_collection("1.0.0"), _collection("1.10.0")],
            ),
            patch.object(
                oci,
                "list_collection_recipes",
                return_value=[_listed("Alpha (one)"), _listed("beta")],
            ) as lister,
            patch.object(oci, "get_registry", return_value=REG),
            patch.object(
                oci,
                "_collection_layout",
                return_value=_pulled(("alpha-one.yaml", ALPHA_V2), ("beta.yaml", BETA)),
            ),
        ):
            state = oci.collection_state("spark-recipes")

        # 1.10.0 is newer than 1.0.0 by number, not by string.
        assert lister.call_args.kwargs["version"] == "1.10.0"
        assert state["latest_version"] == "1.10.0"
        assert state["display_version"] == "v1.10.0"
        assert state["installed_version"] == "1.0.0"
        assert state["checked"] is True
        # The recipe installed from another collection is not this one's.
        assert {r["name"]: r["state"] for r in state["recipes"]} == {
            "Alpha (one)": "update",
            "beta": "not_installed",
        }

    def test_an_unreadable_newest_version_says_so(self, recipes_dir):
        _install(recipes_dir, "alpha-one.yaml", ALPHA_V1)
        with (
            patch.object(oci, "list_collections", return_value=[_collection("1.0.0")]),
            patch.object(
                oci, "list_collection_recipes", return_value=[_listed("Alpha (one)")]
            ),
            patch.object(oci, "get_registry", return_value=REG),
            patch.object(oci, "_collection_layout", side_effect=RuntimeError("down")),
        ):
            state = oci.collection_state("spark-recipes")
        assert state["checked"] is False
        assert state["recipes"][0]["state"] == "installed"

    def test_a_collection_nobody_offers_is_refused(self, recipes_dir):
        with patch.object(oci, "list_collections", return_value=[]):
            with pytest.raises(ValueError, match="ghost"):
                oci.collection_state("ghost")


class TestCollectionLayout:
    def test_a_pulled_version_is_read_from_disk_the_second_time(self, recipes_dir):
        index = {"manifests": [{"digest": "sha256:m1"}]}

        def pull(url, tag, layout, auth=None):
            layout.mkdir(parents=True, exist_ok=True)
            (layout / "alpha.yaml").write_text(ALPHA_V1)

        with (
            patch.object(oci, "_fetch_oci_index", return_value=index),
            patch.object(oci, "_pull_oci_to_layout", side_effect=pull) as puller,
        ):
            first = oci._collection_layout(REG, "spark-recipes", "1.0.0")
            second = oci._collection_layout(REG, "spark-recipes", "1.0.0")

        assert puller.call_count == 1
        assert first == second
        assert first[0]["digest"] == _digest(ALPHA_V1)

    def test_a_short_pull_is_not_cached(self, recipes_dir):
        index = {"manifests": [{"digest": "sha256:m1"}, {"digest": "sha256:m2"}]}

        def pull(url, tag, layout, auth=None):
            layout.mkdir(parents=True, exist_ok=True)
            (layout / "alpha.yaml").write_text(ALPHA_V1)

        with (
            patch.object(oci, "_fetch_oci_index", return_value=index),
            patch.object(oci, "_pull_oci_to_layout", side_effect=pull) as puller,
        ):
            oci._collection_layout(REG, "spark-recipes", "1.0.0")
            oci._collection_layout(REG, "spark-recipes", "1.0.0")
        assert puller.call_count == 2

    def test_a_re_pushed_tag_is_a_different_directory(self, recipes_dir):
        seen = []

        def pull(url, tag, layout, auth=None):
            seen.append(layout)
            layout.mkdir(parents=True, exist_ok=True)
            (layout / "alpha.yaml").write_text(ALPHA_V1)

        with (
            patch.object(
                oci,
                "_fetch_oci_index",
                side_effect=[
                    {"manifests": [{"digest": "sha256:a"}]},
                    {"manifests": [{"digest": "sha256:b"}]},
                ],
            ),
            patch.object(oci, "_pull_oci_to_layout", side_effect=pull),
        ):
            oci._collection_layout(REG, "spark-recipes", "latest")
            oci._collection_layout(REG, "spark-recipes", "latest")
        assert len(set(seen)) == 2


# ── Apply ────────────────────────────────────────────────────────────────────


class TestApplyCollectionRecipes:
    def _apply(self, names, overwrite_local=False, pulled=None):
        pulled = pulled or _pulled(("alpha-one.yaml", ALPHA_V2), ("beta.yaml", BETA))
        with (
            patch.object(oci, "list_collections", return_value=[_collection("1.1.0")]),
            patch.object(oci, "get_registry", return_value=REG),
            patch.object(oci, "_collection_layout", return_value=pulled),
        ):
            return oci.apply_collection_recipes(
                "spark-recipes", names, overwrite_local=overwrite_local
            )

    def test_one_result_per_recipe_and_a_failure_does_not_stop_the_rest(
        self, recipes_dir
    ):
        result = self._apply(["Alpha (one)", "missing", "beta"])

        assert result["version"] == "1.1.0"
        assert [(r["recipe"], r["success"]) for r in result["results"]] == [
            ("Alpha (one)", True),
            ("missing", False),
            ("beta", True),
        ]
        assert "missing" in result["results"][1]["error"]
        assert (recipes_dir / "alpha-one.yaml").read_text() == ALPHA_V2
        meta = oci._read_recipe_meta("alpha-one.yaml")
        assert meta.digest == _digest(ALPHA_V2)
        assert meta.display_name == "Alpha (one)"
        assert meta.version == "1.1.0"
        assert result["results"][0]["recipe_id"] == "oci-alpha-one"

    def test_an_installed_recipe_is_updated(self, recipes_dir):
        _install(recipes_dir, "alpha-one.yaml", ALPHA_V1)
        result = self._apply(["Alpha (one)"])
        assert result["results"][0]["action"] == "updated"

    def test_the_same_content_is_up_to_date(self, recipes_dir):
        _install(recipes_dir, "beta.yaml", BETA)
        assert self._apply(["beta"])["results"][0]["action"] == "up_to_date"

    def test_local_edits_are_skipped_unless_overwrite_is_asked(self, recipes_dir):
        _install(recipes_dir, "alpha-one.yaml", ALPHA_V1)
        (recipes_dir / "alpha-one.yaml").write_text(ALPHA_V1 + "# mine\n")

        skipped = self._apply(["Alpha (one)"])
        assert skipped["results"][0] == {
            "recipe": "Alpha (one)",
            "recipe_id": "oci-alpha-one",
            "success": True,
            "action": "skipped_local_edits",
        }
        assert (recipes_dir / "alpha-one.yaml").read_text().endswith("# mine\n")

        replaced = self._apply(["Alpha (one)"], overwrite_local=True)
        assert replaced["results"][0]["action"] == "updated"
        assert (recipes_dir / "alpha-one.yaml").read_text() == ALPHA_V2

    def test_a_file_put_there_by_hand_is_somebodys_edit(self, recipes_dir):
        (recipes_dir / "beta.yaml").write_text("name: beta\n# by hand\n")
        result = self._apply(["beta"])
        assert result["results"][0]["action"] == "skipped_local_edits"

    def test_a_pinned_version_is_pulled_from_the_named_registry(self, recipes_dir):
        with (
            patch.object(oci, "get_registry", return_value=REG) as getter,
            patch.object(
                oci, "_collection_layout", return_value=_pulled(("beta.yaml", BETA))
            ) as layout,
        ):
            result = oci.apply_collection_recipes(
                "spark-recipes", ["beta"], version="0.9.0", registry_name="ghcr"
            )
        assert result["version"] == "0.9.0"
        assert getter.call_args.args == ("ghcr",)
        assert layout.call_args.args == (REG, "spark-recipes", "0.9.0")

    def test_a_registry_nobody_configured_is_refused(self, recipes_dir):
        with patch.object(oci, "get_registry", return_value=None):
            with pytest.raises(ValueError):
                oci.apply_collection_recipes(
                    "spark-recipes", ["beta"], version="1", registry_name="x"
                )
