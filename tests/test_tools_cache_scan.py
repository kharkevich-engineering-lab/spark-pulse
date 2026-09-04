"""Tests for scanning and clearing the caches the Cache page manages.

``tests/test_tools_cache.py`` covers the directory table and a single scan.
What it does not cover is the part that deletes things: ``clean_cache`` walks a
directory the operator picked by *name* and unlinks everything in it. Every
test here points that machinery at a tmp_path — nothing may touch a real
``~/.cache``.
"""

from __future__ import annotations

import sys

import pytest

from spark_pulse import tools as _tools_pkg

# Reaching the real module takes two steps and one apology. The tools package
# attribute is whichever twin the simulation switch installed, so sys.modules
# is what holds the submodule itself (the idiom conftest.py uses) — and the
# import that puts it there also overwrites the package attribute, which would
# quietly hand the mock's callers the real module for the rest of the process.
# So: remember the switch's choice, import, put it back.
_switched = _tools_pkg.cache
import spark_pulse.tools.cache  # noqa: E402,F401

cache = sys.modules["spark_pulse.tools.cache"]
_tools_pkg.cache = _switched


@pytest.fixture
def caches(tmp_path, monkeypatch):
    """Two cache directories the tests own, in place of the real table."""
    hf = tmp_path / "hf"
    hf.mkdir()
    (hf / "model.bin").write_bytes(b"x" * 100)
    nested = hf / "blobs"
    nested.mkdir()
    (nested / "blob").write_bytes(b"y" * 50)

    vllm = tmp_path / "vllm"

    table = [
        {"name": "HF Model Cache", "path": str(hf), "description": "Models"},
        {"name": "vLLM Cache", "path": str(vllm), "description": "Compiled graphs"},
    ]
    monkeypatch.setattr(cache, "get_cache_dirs", lambda: list(table))
    return {"hf": hf, "vllm": vllm}


# ── Scanning ─────────────────────────────────────────────────────────────────


class TestScanDir:
    def test_files_are_counted_through_subdirectories(self, caches):
        scanned = cache.scan_dir(str(caches["hf"]))

        assert scanned["file_count"] == 2
        assert scanned["size_bytes"] == 150
        assert scanned["path"] == str(caches["hf"])
        assert scanned["name"] == "hf"

    def test_a_directory_that_was_never_created_scans_as_empty(self, caches):
        scanned = cache.scan_dir(str(caches["vllm"]))

        assert scanned["size_bytes"] == 0
        assert scanned["file_count"] == 0

    def test_a_file_that_vanishes_mid_scan_does_not_abort_the_scan(
        self, caches, monkeypatch
    ):
        """rglob yields a path; stat() on it can still fail. Keep counting."""
        real_stat = cache.Path.stat

        def flaky_stat(self, *args, **kwargs):
            if self.name == "model.bin":
                raise OSError("vanished")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(cache.Path, "stat", flaky_stat)

        scanned = cache.scan_dir(str(caches["hf"]))

        assert scanned["file_count"] == 1
        assert scanned["size_bytes"] == 50

    def test_a_directory_that_cannot_be_walked_scans_as_empty(
        self, caches, monkeypatch
    ):
        def denied(self, pattern):
            raise OSError("permission denied")

        monkeypatch.setattr(cache.Path, "rglob", denied)

        assert cache.scan_dir(str(caches["hf"]))["file_count"] == 0


class TestListCache:
    def test_every_directory_is_scanned_and_keeps_its_description(self, caches):
        entries = cache.list_cache()

        assert [e["name"] for e in entries] == ["hf", "vllm"]
        assert [e["description"] for e in entries] == ["Models", "Compiled graphs"]
        assert entries[0]["size_bytes"] == 150
        assert entries[1]["size_bytes"] == 0


# ── Cleaning ─────────────────────────────────────────────────────────────────


class TestCleanCache:
    def test_a_named_cache_is_emptied_but_not_removed(self, caches):
        results = cache.clean_cache(["HF Model Cache"])

        assert results == {"HF Model Cache": f"Cleaned {caches['hf']}"}
        assert caches["hf"].is_dir()
        assert list(caches["hf"].iterdir()) == []

    def test_all_means_every_cache_in_the_table(self, caches):
        results = cache.clean_cache(["all"])

        assert set(results) == {"HF Model Cache", "vLLM Cache"}
        assert list(caches["hf"].iterdir()) == []

    def test_a_cache_directory_that_does_not_exist_says_so(self, caches):
        assert cache.clean_cache(["vLLM Cache"]) == {
            "vLLM Cache": "Cache directory does not exist"
        }

    def test_an_unknown_name_deletes_nothing_and_says_so(self, caches):
        assert cache.clean_cache(["Bitcoin"]) == {"Bitcoin": "Unknown cache: Bitcoin"}
        assert (caches["hf"] / "model.bin").exists()

    def test_a_directory_that_will_not_open_reports_the_error(
        self, caches, monkeypatch
    ):
        def denied(self):
            raise OSError("permission denied")

        monkeypatch.setattr(cache.Path, "iterdir", denied)

        results = cache.clean_cache(["HF Model Cache"])

        assert results["HF Model Cache"].startswith("Error: ")
        assert "permission denied" in results["HF Model Cache"]


# ── The simulation twin ──────────────────────────────────────────────────────


class TestMockCache:
    """``mock/cache.py`` is what the Cache page renders in the e2e suite."""

    def test_it_exposes_every_public_name_the_real_module_does(self):
        from spark_pulse.mock import cache as mock_cache

        expected = {
            name
            for name, value in vars(cache).items()
            if not name.startswith("_")
            and getattr(value, "__module__", None) == cache.__name__
        }

        assert expected == {"get_cache_dirs", "scan_dir", "list_cache", "clean_cache"}
        assert {n for n in expected if not hasattr(mock_cache, n)} == set()

    def test_the_canned_entries_have_the_shape_the_page_reads(self):
        from spark_pulse.mock import cache as mock_cache

        entries = mock_cache.list_cache()

        assert [e["name"] for e in entries] == [
            d["name"] for d in mock_cache.get_cache_dirs()
        ]
        for entry in entries:
            assert entry["size_bytes"] > 0
            assert entry["file_count"] > 0
            assert entry["description"]

    def test_cleaning_reports_a_result_per_target(self):
        from spark_pulse.mock import cache as mock_cache

        assert mock_cache.clean_cache(["CCache", "vLLM Cache"]) == {
            "CCache": "Mock: cleaned CCache",
            "vLLM Cache": "Mock: cleaned vLLM Cache",
        }

    def test_a_simulated_scan_measures_a_real_directory(self, tmp_path):
        from spark_pulse.mock import cache as mock_cache

        (tmp_path / "a").write_bytes(b"z" * 7)

        assert mock_cache.scan_dir(str(tmp_path)) == {
            "size_bytes": 7,
            "file_count": 1,
        }
        assert mock_cache.scan_dir(str(tmp_path / "gone")) == {
            "size_bytes": 0,
            "file_count": 0,
        }
