import importlib

from spark_pulse.tools import cache

# The real module: production is what has to stop shelling out to the checkout.
real_cache = importlib.import_module("spark_pulse.tools.cache")


def test_get_cache_dirs_contains_expected_entries(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/home")

    entries = cache.get_cache_dirs()
    names = {entry["name"] for entry in entries}

    assert "HF Model Cache" in names
    assert "Wheels (spark-vllm)" in names


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


# ── The checkout is optional, and nothing is executed out of it ──────────────


class TestWheelsCacheEntry:
    """``hf-download.sh --cleanup`` is gone; the wheels dir is just a directory.

    It also belongs to a checkout, so it is offered only when there is one —
    a permanently empty entry an operator cannot clean is worse than no entry.
    """

    def test_the_wheels_entry_appears_only_with_a_checkout(self, tmp_path, monkeypatch):
        import spark_pulse.config as cfg

        monkeypatch.setitem(cfg.config._data, "spark_vllm_path", str(tmp_path))
        names = {entry["name"] for entry in real_cache.get_cache_dirs()}
        assert real_cache.WHEELS_CACHE_NAME in names

        monkeypatch.setitem(
            cfg.config._data, "spark_vllm_path", str(tmp_path / "absent")
        )
        names = {entry["name"] for entry in real_cache.get_cache_dirs()}
        assert real_cache.WHEELS_CACHE_NAME not in names

    def test_cleaning_the_wheels_dir_deletes_files_without_a_subprocess(
        self, tmp_path, monkeypatch
    ):
        import spark_pulse.config as cfg

        monkeypatch.setitem(cfg.config._data, "spark_vllm_path", str(tmp_path))
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        (wheels / "a.whl").write_text("x")

        def _no_subprocess(*_a, **_k):  # pragma: no cover - must never run
            raise AssertionError("clean_cache must not shell out to the checkout")

        monkeypatch.setattr("subprocess.run", _no_subprocess)

        result = real_cache.clean_cache([real_cache.WHEELS_CACHE_NAME])

        assert "Cleaned" in result[real_cache.WHEELS_CACHE_NAME]
        assert list(wheels.iterdir()) == []
