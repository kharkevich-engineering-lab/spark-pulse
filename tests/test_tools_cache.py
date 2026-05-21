from spark_pulse.tools import cache


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
