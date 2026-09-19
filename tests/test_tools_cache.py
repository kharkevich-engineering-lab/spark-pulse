import importlib

from spark_pulse.tools import cache

# The real module: production is what has to list the caches an engine fills.
real_cache = importlib.import_module("spark_pulse.tools.cache")


def test_get_cache_dirs_contains_expected_entries(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/home")

    entries = cache.get_cache_dirs()
    names = {entry["name"] for entry in entries}

    assert "HF Model Cache" in names


def test_scan_dir_returns_zero_for_missing_path(tmp_path):
    missing = tmp_path / "missing"

    out = cache.scan_dir(str(missing))

    assert out["size_bytes"] == 0
    assert out["file_count"] == 0


def test_scan_dir_counts_files_and_sizes(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    (d / "a.bin").write_bytes(b"1234")
    sub = d / "nested"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"12")

    out = cache.scan_dir(str(d))

    assert out["file_count"] == 2
    assert out["size_bytes"] == 6


# ── Only the caches an engine fills ──────────────────────────────────────────


class TestOnlyRuntimeCaches:
    """The build caches are gone, because the build they belong to is not ours.

    ``wheels`` lived in a spark-vllm-docker checkout, and ``.ccache`` and ``uv``
    are what compiling those wheels fills. Spark Pulse has never compiled one —
    engines arrive as images — so all three were entries an operator was invited
    to clean on behalf of a workflow this product does not run.
    """

    def test_the_listed_caches_are_the_engine_runtime_ones(self, monkeypatch):
        monkeypatch.setenv("HOME", "/tmp/home")

        names = {entry["name"] for entry in real_cache.get_cache_dirs()}

        assert names == {
            "HF Model Cache",
            "vLLM Cache",
            "FlashInfer Cache",
            "Triton Cache",
        }

    def test_no_entry_points_into_a_checkout(self, monkeypatch):
        monkeypatch.setenv("HOME", "/tmp/home")

        paths = [entry["path"] for entry in real_cache.get_cache_dirs()]

        assert not any("spark-vllm-docker" in path for path in paths)
        assert not any(path.endswith("/wheels") for path in paths)

    def test_cleaning_a_cache_deletes_files_without_a_subprocess(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        triton = tmp_path / ".triton"
        triton.mkdir()
        (triton / "a.cubin").write_text("x")

        def _no_subprocess(*_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("clean_cache must not shell out")

        monkeypatch.setattr("subprocess.run", _no_subprocess)

        result = real_cache.clean_cache(["Triton Cache"])

        assert "Cleaned" in result["Triton Cache"]
        assert list(triton.iterdir()) == []
