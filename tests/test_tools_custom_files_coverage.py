"""The corners of custom-file handling: directories, bad bytes and bad ids.

``tests/test_tools_custom_files.py`` covers the happy single-file path. What is
left is everything that only shows up on a real operator's disk: a recipe kept
in a subdirectory, a file that is not text, a mod whose ``run.sh`` cannot be
read, and every id that must be refused before it can escape the config
directory.

Every test here works inside ``tmp_path``. The module reads and writes the
operator's own ``~/.config/spark-pulse`` in both real and simulation mode, so
the fixture repoints both directory globals before anything runs — the
autouse fixture in ``conftest.py`` does the same, and this one keeps the file
honest on its own.
"""

from __future__ import annotations

import pytest

from spark_pulse.tools import custom_files


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Point both config directories at this test's own tmp_path."""
    recipes = tmp_path / "custom-recipes"
    mods = tmp_path / "custom-mods"
    monkeypatch.setattr(custom_files, "_CUSTOM_RECIPES_DIR", recipes)
    monkeypatch.setattr(custom_files, "_CUSTOM_MODS_DIR", mods)
    recipes.mkdir()
    mods.mkdir()
    return recipes, mods


# ── Path safety ──────────────────────────────────────────────────────────────


class TestPathSafety:
    """Nothing an id says may reach outside the two config directories."""

    @pytest.mark.parametrize(
        "part, safe",
        [
            ("recipe", True),
            ("with-dash_and.dot", True),
            ("", False),
            (".", False),
            ("..", False),
            ("a/b", False),
            ("a\\b", False),
        ],
    )
    def test_path_part_rules(self, part, safe):
        assert custom_files._is_safe_path_part(part) is safe

    @pytest.mark.parametrize(
        "rel, safe",
        [
            ("run.sh", True),
            ("nested/dir/file.txt", True),
            ("/etc/passwd", False),
            ("../escape.txt", False),
            ("nested/../../escape", False),
            # ``Path`` normalises both of these away to no parts at all.
            (".", False),
            ("", False),
        ],
    )
    def test_relative_path_rules(self, rel, safe):
        assert custom_files._is_safe_rel_path(rel) is safe

    def test_the_directory_accessors_read_through_to_the_globals(
        self, sandbox, tmp_path, monkeypatch
    ):
        """Callers must never cache these — a moved directory has to be seen."""
        recipes, mods = sandbox
        assert custom_files.custom_recipes_dir() == recipes
        assert custom_files.custom_mods_dir() == mods

        monkeypatch.setattr(custom_files, "_CUSTOM_RECIPES_DIR", tmp_path / "moved")
        assert custom_files.custom_recipes_dir() == tmp_path / "moved"


# ── Directory recipes ────────────────────────────────────────────────────────


class TestDirectoryRecipes:
    """A recipe kept as ``<dir>/<name>.yaml`` rather than a loose file."""

    def test_a_lone_directory_recipe_is_listed(self, sandbox):
        """Regression: this used to raise UnboundLocalError.

        ``import yaml`` sat inside the loose-file branch, which made the name
        local to the whole function. A custom-recipes directory whose first
        entry was a subdirectory — the only entry, here — blew up on
        ``yaml.safe_load`` and took every recipe listing down with it.
        """
        recipes_dir, _ = sandbox
        stack = recipes_dir / "mystack"
        stack.mkdir()
        (stack / "recipe.yaml").write_text("name: My Stack\nmodel: foo\n")

        found = custom_files.discover_custom_recipes()

        assert [r["id"] for r in found] == ["custom/mystack/recipe"]
        assert found[0]["name"] == "My Stack"
        assert found[0]["filename"] == "recipe.yaml"
        assert found[0]["filepath"] == str(stack / "recipe.yaml")

    def test_a_yml_file_in_a_directory_counts(self, sandbox):
        recipes_dir, _ = sandbox
        stack = recipes_dir / "ymlstack"
        stack.mkdir()
        (stack / "inner.yml").write_text("name: Yml Stack\n")

        found = custom_files.discover_custom_recipes()

        assert [r["id"] for r in found] == ["custom/ymlstack/inner"]
        assert found[0]["name"] == "Yml Stack"

    def test_a_directory_without_yaml_is_not_a_recipe(self, sandbox):
        recipes_dir, _ = sandbox
        (recipes_dir / "notes").mkdir()
        (recipes_dir / "notes" / "README.md").write_text("hello")

        assert custom_files.discover_custom_recipes() == []

    def test_an_unreadable_directory_recipe_is_skipped_not_fatal(self, sandbox):
        """One bad entry must not cost the operator the rest of the listing."""
        recipes_dir, _ = sandbox
        broken = recipes_dir / "broken-dir"
        broken.mkdir()
        # A *directory* named like a recipe file: it matches the glob and then
        # refuses to be opened (IsADirectoryError).
        (broken / "recipe.yaml").mkdir()
        (recipes_dir / "good.yaml").write_text("name: Good\n")

        found = custom_files.discover_custom_recipes()

        assert [r["id"] for r in found] == ["custom/good"]

    def test_an_empty_directory_recipe_falls_back_to_the_directory_name(self, sandbox):
        recipes_dir, _ = sandbox
        stack = recipes_dir / "nameless"
        stack.mkdir()
        (stack / "recipe.yaml").write_text("")

        found = custom_files.discover_custom_recipes()

        assert found[0]["name"] == "nameless"

    def test_get_content_of_a_directory_recipe(self, sandbox):
        recipes_dir, _ = sandbox
        stack = recipes_dir / "sub"
        stack.mkdir()
        (stack / "inner.yml").write_text("name: Inner\n")

        got = custom_files.get_custom_recipe_content("custom/sub/inner")

        assert got == {"content": "name: Inner\n", "id": "custom/sub/inner"}

    def test_get_content_of_a_missing_file_in_an_existing_directory(self, sandbox):
        recipes_dir, _ = sandbox
        (recipes_dir / "sub").mkdir()

        assert custom_files.get_custom_recipe_content("custom/sub/nope") is None

    def test_get_content_refuses_traversal(self, sandbox):
        assert custom_files.get_custom_recipe_content("custom/../../etc/passwd") is None

    def test_get_content_refuses_a_three_part_id(self, sandbox):
        assert custom_files.get_custom_recipe_content("custom/a/b/c") is None

    def test_save_creates_the_recipe_subdirectory(self, sandbox):
        recipes_dir, _ = sandbox

        assert custom_files.save_custom_recipe("custom/sub/inner", "name: In\n") is True

        assert (recipes_dir / "sub" / "inner.yaml").read_text() == "name: In\n"

    @pytest.mark.parametrize("bad_id", ["custom/a/b/c", "custom/", "custom/./x"])
    def test_save_refuses_an_unusable_id(self, sandbox, bad_id):
        with pytest.raises(ValueError, match="Invalid recipe id"):
            custom_files.save_custom_recipe(bad_id, "name: X\n")

    def test_delete_a_directory_recipe_keeps_the_directory(self, sandbox):
        recipes_dir, _ = sandbox
        stack = recipes_dir / "sub"
        stack.mkdir()
        (stack / "inner.yml").write_text("name: Inner\n")

        assert custom_files.delete_custom_recipe("custom/sub/inner") is True
        assert not (stack / "inner.yml").exists()
        assert stack.is_dir()

    def test_delete_a_missing_directory_recipe(self, sandbox):
        recipes_dir, _ = sandbox
        (recipes_dir / "sub").mkdir()

        assert custom_files.delete_custom_recipe("custom/sub/nope") is False

    def test_delete_when_the_directory_itself_is_absent(self, sandbox):
        assert custom_files.delete_custom_recipe("custom/gone/inner") is False

    def test_delete_refuses_traversal(self, sandbox, tmp_path):
        victim = tmp_path / "victim.yaml"
        victim.write_text("name: Victim\n")

        assert custom_files.delete_custom_recipe("custom/../victim") is False
        assert victim.exists()

    def test_delete_finds_a_yml_extension(self, sandbox):
        recipes_dir, _ = sandbox
        (recipes_dir / "loose.yml").write_text("name: Loose\n")

        assert custom_files.delete_custom_recipe("custom/loose") is True
        assert not (recipes_dir / "loose.yml").exists()


# ── Upload ───────────────────────────────────────────────────────────────────


class TestUploadCustomRecipe:
    """Uploading is the only entry point that takes raw bytes."""

    def test_bytes_are_stored_verbatim_under_the_stem(self, sandbox):
        recipes_dir, _ = sandbox

        result = custom_files.upload_custom_recipe(
            b"name: Uploaded\nmodel: foo\n", "uploaded.yaml"
        )

        assert result == {"id": "custom/uploaded", "name": "Uploaded"}
        assert (recipes_dir / "uploaded.yaml").read_bytes() == (
            b"name: Uploaded\nmodel: foo\n"
        )

    def test_a_yml_upload_is_normalised_to_yaml(self, sandbox):
        recipes_dir, _ = sandbox

        result = custom_files.upload_custom_recipe(b"name: Norm\n", "norm.yml")

        assert result["id"] == "custom/norm"
        assert (recipes_dir / "norm.yaml").exists()
        assert not (recipes_dir / "norm.yml").exists()

    def test_a_nameless_recipe_falls_back_to_the_filename(self, sandbox):
        result = custom_files.upload_custom_recipe(b"model: foo\n", "fallback.yaml")

        assert result["name"] == "fallback"

    def test_an_existing_directory_of_the_same_name_is_refused(self, sandbox):
        recipes_dir, _ = sandbox
        (recipes_dir / "clash").mkdir()

        with pytest.raises(ValueError, match="already exists"):
            custom_files.upload_custom_recipe(b"name: Clash\n", "clash.yaml")

        assert not (recipes_dir / "clash.yaml").exists()

    def test_malformed_yaml_is_refused_and_nothing_is_written(self, sandbox):
        recipes_dir, _ = sandbox

        with pytest.raises(ValueError, match="Invalid YAML"):
            custom_files.upload_custom_recipe(b"name: [[[unclosed\n", "bad.yaml")

        assert not (recipes_dir / "bad.yaml").exists()

    def test_a_yaml_list_is_refused(self, sandbox):
        recipes_dir, _ = sandbox

        with pytest.raises(ValueError, match="must be a mapping"):
            custom_files.upload_custom_recipe(b"- one\n- two\n", "list.yaml")

        assert not (recipes_dir / "list.yaml").exists()


# ── Mods ─────────────────────────────────────────────────────────────────────


class TestModDiscovery:
    """A mod is a directory with a ``run.sh``; anything else is not a mod."""

    def test_hidden_directories_and_loose_files_are_skipped(self, sandbox):
        _, mods_dir = sandbox
        hidden = mods_dir / ".hidden-mod"
        hidden.mkdir()
        (hidden / "run.sh").write_text("#!/bin/sh\n")
        (mods_dir / "loose.sh").write_text("#!/bin/sh\n")
        real = mods_dir / "real-mod"
        real.mkdir()
        (real / "run.sh").write_text("#!/bin/sh\n")

        found = custom_files.discover_custom_mods()

        assert [m["id"] for m in found] == ["custom/real-mod"]

    def test_an_unreadable_run_sh_leaves_the_description_empty(self, sandbox):
        """The mod still exists — it just has nothing to say about itself."""
        _, mods_dir = sandbox
        mod = mods_dir / "opaque"
        mod.mkdir()
        # A directory named run.sh: it exists, so the mod is valid, but opening
        # it raises IsADirectoryError.
        (mod / "run.sh").mkdir()

        found = custom_files.discover_custom_mods()

        assert found == [
            {
                "id": "custom/opaque",
                "name": "opaque",
                "description": "",
                "filepath": str(mod),
                "has_run_sh": True,
            }
        ]

    def test_a_run_sh_without_comments_has_no_description(self, sandbox):
        _, mods_dir = sandbox
        mod = mods_dir / "silent"
        mod.mkdir()
        (mod / "run.sh").write_text("#!/bin/bash\necho hi\n")

        assert custom_files.discover_custom_mods()[0]["description"] == ""


class TestModFiles:
    """Reading and writing the files inside one mod."""

    def test_nested_files_are_keyed_by_their_relative_path(self, sandbox):
        _, mods_dir = sandbox
        mod = mods_dir / "nested"
        (mod / "templates").mkdir(parents=True)
        (mod / "run.sh").write_text("#!/bin/sh\n")
        (mod / "templates" / "t.jinja").write_text("{{ x }}")

        got = custom_files.get_custom_mod_files("custom/nested")

        assert got["id"] == "custom/nested"
        assert got["files"] == {
            "run.sh": "#!/bin/sh\n",
            "templates/t.jinja": "{{ x }}",
        }

    def test_hidden_files_are_left_out(self, sandbox):
        _, mods_dir = sandbox
        mod = mods_dir / "dotted"
        mod.mkdir()
        (mod / "run.sh").write_text("#!/bin/sh\n")
        (mod / ".secret").write_text("token")

        got = custom_files.get_custom_mod_files("custom/dotted")

        assert list(got["files"]) == ["run.sh"]

    def test_a_binary_file_is_skipped_and_the_rest_survive(self, sandbox):
        _, mods_dir = sandbox
        mod = mods_dir / "binary"
        mod.mkdir()
        (mod / "run.sh").write_text("#!/bin/sh\n")
        (mod / "blob.bin").write_bytes(b"\xff\xfe\x00\x01not utf-8")

        got = custom_files.get_custom_mod_files("custom/binary")

        assert list(got["files"]) == ["run.sh"]

    def test_an_unknown_mod_is_none(self, sandbox):
        assert custom_files.get_custom_mod_files("custom/nope") is None

    @pytest.mark.parametrize("bad_id", ["custom/a/b", "custom/..", "custom/"])
    def test_an_unusable_mod_id_is_none(self, sandbox, bad_id):
        assert custom_files.get_custom_mod_files(bad_id) is None

    def test_saving_creates_nested_directories(self, sandbox):
        _, mods_dir = sandbox

        assert (
            custom_files.save_custom_mod(
                "custom/deep", {"templates/inner/t.jinja": "{{ y }}"}
            )
            is True
        )

        assert (mods_dir / "deep" / "templates" / "inner" / "t.jinja").read_text() == (
            "{{ y }}"
        )

    def test_saving_overwrites_an_existing_file(self, sandbox):
        _, mods_dir = sandbox
        mod = mods_dir / "over"
        mod.mkdir()
        (mod / "run.sh").write_text("old\n")

        custom_files.save_custom_mod("custom/over", {"run.sh": "new\n"})

        assert (mod / "run.sh").read_text() == "new\n"

    def test_saving_refuses_an_absolute_file_path(self, sandbox, tmp_path):
        outside = tmp_path / "outside.txt"

        with pytest.raises(ValueError, match="Invalid mod file path"):
            custom_files.save_custom_mod("custom/abs", {str(outside): "oops"})

        assert not outside.exists()

    @pytest.mark.parametrize("name", [".", ""])
    def test_saving_refuses_a_file_name_that_is_the_directory_itself(
        self, sandbox, name
    ):
        """Regression: these used to reach ``write_text`` on the mod directory.

        ``Path(".")`` and ``Path("")`` both carry no parts, so the per-part
        check waved them through and the write raised IsADirectoryError — a
        500 from the router for what is a rejected file name.
        """
        _, mods_dir = sandbox

        with pytest.raises(ValueError, match="Invalid mod file path"):
            custom_files.save_custom_mod("custom/dot", {name: "oops"})

        assert (
            not (mods_dir / "dot").exists() or list((mods_dir / "dot").iterdir()) == []
        )

    @pytest.mark.parametrize("bad_id", ["custom/a/b", "custom/..", "custom/"])
    def test_saving_refuses_an_unusable_mod_id(self, sandbox, bad_id):
        with pytest.raises(ValueError, match="Invalid mod id"):
            custom_files.save_custom_mod(bad_id, {"run.sh": "#!/bin/sh\n"})

    @pytest.mark.parametrize("bad_id", ["custom/a/b", "custom/..", "custom/"])
    def test_deleting_refuses_an_unusable_mod_id(self, sandbox, bad_id):
        assert custom_files.delete_custom_mod(bad_id) is False

    def test_deleting_a_traversing_id_leaves_the_target_alone(self, sandbox, tmp_path):
        victim = tmp_path / "victim"
        victim.mkdir()

        assert custom_files.delete_custom_mod("custom/../victim") is False
        assert victim.is_dir()

    def test_deleting_removes_the_whole_tree(self, sandbox):
        _, mods_dir = sandbox
        custom_files.save_custom_mod(
            "custom/tree", {"run.sh": "#!/bin/sh\n", "a/b/c.txt": "x"}
        )

        assert custom_files.delete_custom_mod("custom/tree") is True
        assert not (mods_dir / "tree").exists()
