"""Importing recipes and mods out of an upstream-layout checkout."""

import json
import subprocess
from pathlib import Path

import pytest

from spark_pulse.tools import recipe_import

VALID_V1 = """
name: TinyLlama
container: vllm-node
model: TinyLlama/TinyLlama-1.1B
command: vllm serve TinyLlama/TinyLlama-1.1B --port {port}
defaults:
  port: 8000
""".strip()

VALID_V2 = """
recipe_version: "2"
name: Structured
model: org/structured
engine: vllm
params:
  port: 8100
engines:
  vllm:
    args: --enable-prefix-caching
""".strip()

INVALID = "name: MissingEverythingElse"


@pytest.fixture
def upstream(tmp_path):
    """A directory shaped like a spark-vllm-docker checkout."""
    root = tmp_path / "upstream"
    recipes = root / "recipes"
    (recipes / "cluster").mkdir(parents=True)
    (recipes / "tiny.yaml").write_text(VALID_V1, encoding="utf-8")
    (recipes / "cluster" / "structured.yml").write_text(VALID_V2, encoding="utf-8")
    (recipes / "broken.yaml").write_text(INVALID, encoding="utf-8")

    mods = root / "mods"
    (mods / "nemotron-nano").mkdir(parents=True)
    (mods / "nemotron-nano" / "run.sh").write_text(
        "#!/usr/bin/env bash\necho patching\n", encoding="utf-8"
    )
    (mods / "nemotron-nano" / "patch.diff").write_text("diff\n", encoding="utf-8")
    (mods / "no-runner").mkdir()
    (mods / "no-runner" / "README.md").write_text("nothing here\n", encoding="utf-8")
    (mods / "loose.txt").write_text("not a mod\n", encoding="utf-8")
    return root


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "imported"


class TestImportFromPath:
    def test_copies_valid_recipes_and_reports_the_invalid_one(self, upstream, dest):
        report = recipe_import.import_from_path(upstream, dest)

        by_file = {r["file"]: r for r in report["recipes"]}
        assert by_file["tiny.yaml"]["status"] == "ok"
        assert by_file["tiny.yaml"]["recipe_version"] == "1"
        assert by_file["cluster/structured.yml"]["status"] == "ok"
        assert by_file["cluster/structured.yml"]["recipe_version"] == "2"
        assert by_file["broken.yaml"]["status"] == "error"
        assert "container" in by_file["broken.yaml"]["message"]

        assert report["counts"]["recipes"] == {"ok": 2, "skipped": 0, "error": 1}

    def test_preserves_the_subdirectory_layout(self, upstream, dest):
        recipe_import.import_from_path(upstream, dest)
        assert (dest / "recipes" / "tiny.yaml").is_file()
        assert (dest / "recipes" / "cluster" / "structured.yml").is_file()
        assert not (dest / "recipes" / "broken.yaml").exists()

    def test_does_not_convert_v1_recipes(self, upstream, dest):
        recipe_import.import_from_path(upstream, dest)
        copied = (dest / "recipes" / "tiny.yaml").read_text(encoding="utf-8")
        assert copied == VALID_V1
        assert "recipe_version" not in copied

    def test_ids_are_prefixed_with_the_source_name(self, upstream, dest):
        report = recipe_import.import_from_path(upstream, dest)
        ids = {r["id"] for r in report["recipes"]}
        assert "imported/tiny" in ids
        assert "imported/cluster/structured" in ids

    def test_copies_mods_with_run_sh_and_skips_the_rest(self, upstream, dest):
        report = recipe_import.import_from_path(upstream, dest)
        by_name = {m["name"]: m for m in report["mods"]}

        assert by_name["nemotron-nano"]["status"] == "ok"
        assert by_name["no-runner"]["status"] == "skipped"
        assert by_name["no-runner"]["message"] == "no run.sh"
        assert by_name["loose.txt"]["status"] == "skipped"

        assert (dest / "mods" / "nemotron-nano" / "run.sh").is_file()
        assert (dest / "mods" / "nemotron-nano" / "patch.diff").is_file()
        assert not (dest / "mods" / "no-runner").exists()

    def test_records_provenance_in_the_manifest(self, upstream, dest):
        report = recipe_import.import_from_path(upstream, dest)
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))

        assert manifest == report
        assert manifest["source"] == str(upstream)
        assert manifest["imported_at"]
        assert manifest["git_sha"] is None

    def test_records_the_git_sha_when_the_source_is_a_repo(self, upstream, dest):
        subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)
        subprocess.run(["git", "add", "-A"], cwd=upstream, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
            cwd=upstream,
            check=True,
        )

        manifest = recipe_import.import_from_path(upstream, dest)
        assert manifest["git_sha"] and len(manifest["git_sha"]) == 40

    def test_reimport_replaces_a_previously_copied_mod(self, upstream, dest):
        recipe_import.import_from_path(upstream, dest)
        stale = dest / "mods" / "nemotron-nano" / "stale.txt"
        stale.write_text("old", encoding="utf-8")

        recipe_import.import_from_path(upstream, dest)
        assert not stale.exists()

    def test_missing_source_raises(self, tmp_path, dest):
        with pytest.raises(FileNotFoundError):
            recipe_import.import_from_path(tmp_path / "nope", dest)

    def test_directory_without_the_expected_layout_raises(self, tmp_path, dest):
        (tmp_path / "empty").mkdir()
        with pytest.raises(ValueError, match="neither a 'recipes' nor a 'mods'"):
            recipe_import.import_from_path(tmp_path / "empty", dest)


class TestImportFromGit:
    def test_clones_shallow_then_imports(self, upstream, dest, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "clone" not in cmd:
                # provenance lookups (rev-parse / remote get-url)
                return subprocess.CompletedProcess(cmd, 1, "", "")
            target = Path(cmd[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copytree(upstream, target)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(recipe_import.subprocess, "run", fake_run)

        manifest = recipe_import.import_from_git(
            "https://example.invalid/repo.git", "v1.2.3", dest
        )

        assert calls[0][:4] == ["git", "clone", "--depth", "1"]
        assert "--branch" in calls[0] and "v1.2.3" in calls[0]
        assert manifest["source_url"] == "https://example.invalid/repo.git"
        assert manifest["ref"] == "v1.2.3"
        assert manifest["counts"]["recipes"]["ok"] == 2

    def test_clone_failure_raises(self, dest, monkeypatch):
        monkeypatch.setattr(
            recipe_import.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, "", "not found"),
        )
        with pytest.raises(RuntimeError, match="not found"):
            recipe_import.import_from_git("https://example.invalid/x.git", None, dest)

    def test_empty_url_raises(self, dest):
        with pytest.raises(ValueError):
            recipe_import.import_from_git("  ", None, dest)


class TestStatus:
    def test_status_is_empty_before_any_import(self, dest):
        assert recipe_import.get_import_status(dest) == {"imported": False}

    def test_status_returns_the_manifest(self, upstream, dest):
        recipe_import.import_from_path(upstream, dest)
        status = recipe_import.get_import_status(dest)
        assert status["imported"] is True
        assert status["source"] == str(upstream)

    def test_status_survives_a_corrupt_manifest(self, upstream, dest):
        recipe_import.import_from_path(upstream, dest)
        (dest / "manifest.json").write_text("{not json", encoding="utf-8")
        assert recipe_import.get_import_status(dest) == {"imported": False}

    def test_clear_imported(self, upstream, dest):
        recipe_import.import_from_path(upstream, dest)
        assert recipe_import.clear_imported(dest) is True
        assert recipe_import.clear_imported(dest) is False


class TestIterImportedRecipeFiles:
    def test_lists_nothing_when_nothing_was_imported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(recipe_import, "IMPORTED_DIR", tmp_path / "nope")
        assert recipe_import.iter_imported_recipe_files() == []

    def test_lists_copied_recipes(self, upstream, dest, monkeypatch):
        recipe_import.import_from_path(upstream, dest)
        monkeypatch.setattr(recipe_import, "IMPORTED_DIR", dest)
        names = [p.name for p in recipe_import.iter_imported_recipe_files()]
        assert names == ["structured.yml", "tiny.yaml"]
