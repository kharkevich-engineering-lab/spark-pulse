"""Tests for OCI registry caching and background updater."""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _setup_oci_module(tmp_path, monkeypatch, cache_ttl=2):
    """Helper to set up oci_registry module with a temp cache dir."""
    monkeypatch.setenv("OCI_CACHE_TTL_SECONDS", str(cache_ttl))
    cache_dir = tmp_path / "oci" / "meta_cache"
    cache_dir.mkdir(parents=True)

    import importlib
    import spark_pulse.tools.oci_registry as mod

    importlib.reload(mod)
    # Patch AFTER reload (reload replaces the module object)
    mod.OCI_META_CACHE_DIR = cache_dir
    return mod, cache_dir


class TestOciCache:
    """Tests for OCI meta cache functions."""

    @pytest.fixture(autouse=True)
    def setup_cache(self, tmp_path, monkeypatch):
        """Set up a temporary cache directory and reload module."""
        self.mod, self.cache_dir = _setup_oci_module(tmp_path, monkeypatch)

    def test_write_and_read_cache(self):
        """Cache write and read works correctly."""
        key = "test_reg:sha256:abc123"
        data = {"annotations": {"name": "test"}, "index": {"manifests": []}}

        self.mod._write_cache(key, data)
        result = self.mod._read_cache(key)

        assert result is not None
        assert result["annotations"]["name"] == "test"
        assert result["index"]["manifests"] == []

    def test_read_cache_miss(self):
        """Reading non-existent cache returns None."""
        result = self.mod._read_cache("nonexistent:key")
        assert result is None

    def test_cache_expiry(self):
        """Cache expires after TTL."""
        key = "test_reg:sha256:expired"
        data = {"annotations": {"name": "test"}, "index": {"manifests": []}}

        self.mod._write_cache(key, data)

        # Should be readable immediately
        result = self.mod._read_cache(key)
        assert result is not None

        # Manually set cached_at to 3 seconds ago (TTL is 2s)
        cache_file = self.mod._cache_path(key)
        with open(cache_file) as f:
            cached = json.load(f)
        cached["_cached_at"] = time.time() - 3
        with open(cache_file, "w") as f:
            json.dump(cached, f)

        # Should now be expired
        result = self.mod._read_cache(key)
        assert result is None

    def test_cache_file_removed_on_expiry(self):
        """Expired cache file is deleted."""
        key = "test_reg:sha256:expiry_cleanup"
        data = {"annotations": {"name": "test"}, "index": {"manifests": []}}

        self.mod._write_cache(key, data)
        cache_file = self.mod._cache_path(key)
        assert cache_file.exists()

        # Manually expire the cache
        with open(cache_file) as f:
            cached = json.load(f)
        cached["_cached_at"] = time.time() - 10
        with open(cache_file, "w") as f:
            json.dump(cached, f)

        # Reading expired cache should delete the file
        self.mod._read_cache(key)
        assert not cache_file.exists()

    def test_clear_cache_all(self):
        """Clearing all cache removes all files."""
        self.mod._write_cache("reg1:tag1", {"data": 1})
        self.mod._write_cache("reg2:tag2", {"data": 2})
        self.mod._write_cache("reg3:tag3", {"data": 3})

        # Verify files were created in the patched cache dir
        files = list(self.cache_dir.glob("*.json"))
        assert len(files) == 3

        result = self.mod.clear_oci_cache()
        assert result["cleared"] == 3
        assert len(list(self.cache_dir.glob("*.json"))) == 0

    def test_clear_cache_specific_key(self):
        """Clearing specific cache key removes only that file."""
        self.mod._write_cache("reg1:tag1", {"data": 1})
        self.mod._write_cache("reg2:tag2", {"data": 2})
        self.mod._write_cache("reg3:tag3", {"data": 3})

        result = self.mod.clear_oci_cache("reg2:tag2")
        assert result["cleared"] == 1
        assert len(list(self.cache_dir.glob("*.json"))) == 2

    def test_clear_cache_nonexistent_key(self):
        """Clearing non-existent key returns cleared=0."""
        result = self.mod.clear_oci_cache("nonexistent:key")
        assert result["cleared"] == 0

    def test_clear_cache_no_dir(self):
        """Clearing cache when directory doesn't exist returns cleared=0."""
        with patch.object(self.mod, "OCI_META_CACHE_DIR", Path("/nonexistent")):
            result = self.mod.clear_oci_cache()
            assert result["cleared"] == 0

    def test_cache_key_generation(self):
        """Cache key is generated correctly."""
        key = self.mod._cache_key("my-registry", "sha256:abc123")
        assert key == "my-registry:sha256:abc123"

    def test_cache_path_creates_directory(self):
        """_cache_path creates the cache directory if it doesn't exist."""
        with patch.object(self.mod, "OCI_META_CACHE_DIR", self.cache_dir / "subdir"):
            # Accessing _cache_path should create the directory
            _ = self.mod._cache_path("test:key")
            assert (self.cache_dir / "subdir").exists()

    def test_cache_ttl_config(self, monkeypatch):
        """Cache TTL respects config value."""
        monkeypatch.setenv("OCI_CACHE_TTL_SECONDS", "600")
        import importlib
        import spark_pulse.tools.oci_registry as mod

        importlib.reload(mod)
        assert mod._cache_ttl() == 600

    def test_cache_ttl_default(self, monkeypatch):
        """Cache TTL falls back to default when config unavailable."""
        monkeypatch.delenv("OCI_CACHE_TTL_SECONDS", raising=False)
        with patch("spark_pulse.tools.oci_registry.config") as mock_config:
            mock_config.oci_cache_ttl_seconds = None
            import importlib
            import spark_pulse.tools.oci_registry as mod

            importlib.reload(mod)
            # Should fall back to _DEFAULT_CACHE_TTL (300)
            assert mod._cache_ttl() == 300


class TestOciBackgroundUpdater:
    """Tests for background update checker."""

    @pytest.fixture(autouse=True)
    def reload_module(self):
        """Reload the module to reset background thread state."""
        import importlib
        import spark_pulse.tools.oci_registry as mod

        # Stop any existing background thread
        mod.stop_background_updater()
        importlib.reload(mod)
        self.mod = mod

    def test_start_background_updater(self):
        """Starting background updater creates a daemon thread."""
        self.mod.start_background_updater()
        assert self.mod._background_thread is not None
        assert self.mod._background_thread.is_alive()
        assert self.mod._background_thread.daemon is True
        self.mod.stop_background_updater()

    def test_start_background_updater_already_running(self):
        """Starting when already running does not create duplicate thread."""
        self.mod.start_background_updater()
        first_thread = self.mod._background_thread
        self.mod.start_background_updater()
        assert self.mod._background_thread is first_thread
        self.mod.stop_background_updater()

    def test_stop_background_updater(self):
        """Stopping background updater stops the thread."""
        self.mod.start_background_updater()
        self.mod.stop_background_updater()
        assert self.mod._background_thread is None

    def test_background_check_interval_config(self, monkeypatch):
        """Background check interval respects config value."""
        monkeypatch.setenv("OCI_BACKGROUND_CHECK_INTERVAL_SECONDS", "120")
        import importlib
        import spark_pulse.tools.oci_registry as mod

        importlib.reload(mod)
        assert mod._background_check_interval() == 120

    def test_background_check_interval_default(self, monkeypatch):
        """Background check interval falls back to 900s default."""
        monkeypatch.delenv("OCI_BACKGROUND_CHECK_INTERVAL_SECONDS", raising=False)
        with patch("spark_pulse.tools.oci_registry.config") as mock_config:
            mock_config.oci_background_check_interval_seconds = None
            import importlib
            import spark_pulse.tools.oci_registry as mod

            importlib.reload(mod)
            assert mod._background_check_interval() == 900

    def test_background_loop_no_updates(self, monkeypatch):
        """Background loop handles no updates gracefully."""
        with patch.object(self.mod, "check_updates", return_value=[]):
            with patch.object(self.mod, "_background_stop", autospec=True) as mock_stop:
                mock_stop.is_set.return_value = False
                mock_stop.wait.side_effect = [False, True]
                self.mod._background_update_loop()

    def test_background_loop_with_updates(self, monkeypatch, caplog):
        """Background loop logs updates when found."""
        caplog.set_level("INFO")
        mock_update = MagicMock()
        mock_update.collection = "test-collection"
        mock_update.current_version = "1.0.0"
        mock_update.latest_version = "2.0.0"

        with patch.object(self.mod, "check_updates", return_value=[mock_update]):
            with patch.object(self.mod, "_background_stop", autospec=True) as mock_stop:
                mock_stop.is_set.return_value = False
                mock_stop.wait.side_effect = [False, True]
                self.mod._background_update_loop()

        assert "test-collection" in caplog.text
        assert "1.0.0" in caplog.text
        assert "2.0.0" in caplog.text

    def test_background_loop_handles_exception(self, monkeypatch):
        """Background loop continues after exception."""
        call_count = [0]

        def fail_once(*_):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Network error")
            return []

        with patch.object(self.mod, "check_updates", side_effect=fail_once):
            with patch.object(self.mod, "_background_stop", autospec=True) as mock_stop:
                mock_stop.is_set.return_value = False
                mock_stop.wait.side_effect = [False, True]
                self.mod._background_update_loop()


class TestOciCacheIntegration:
    """Integration tests for cache + list_collections."""

    @pytest.fixture(autouse=True)
    def setup_cache(self, tmp_path, monkeypatch):
        """Set up a temporary cache directory and reload module."""
        self.mod, self.cache_dir = _setup_oci_module(
            tmp_path, monkeypatch, cache_ttl=300
        )

    def test_list_collections_uses_cache(self):
        """list_collections uses cache on second call."""
        mock_index = {
            "annotations": {
                "name": "test-recipes",
                "version": "1.0.0",
                "description": "Test collection",
                "vendor": "Test Vendor",
                "license": "MIT",
            },
            "manifests": [
                {"digest": "sha256:abc", "size": 100},
                {"digest": "sha256:def", "size": 200},
            ],
            "digest": "sha256:manifest-digest",
        }

        call_count = [0]

        def mock_fetch_index(url, tag, auth=None):
            call_count[0] += 1
            return mock_index

        def mock_list_tags(url, auth=None):
            return ["sha256:abc123"]

        with patch.object(self.mod, "_oras_list_tags", side_effect=mock_list_tags):
            with patch.object(
                self.mod, "_fetch_oci_index", side_effect=mock_fetch_index
            ):
                with patch.object(
                    self.mod,
                    "_load_registries",
                    return_value=[
                        {"name": "test-reg", "url": "example.com/repo", "enabled": True}
                    ],
                ):
                    # First call - should fetch from network
                    collections = self.mod.list_collections(registry_name="test-reg")
                    assert call_count[0] == 1
                    assert len(collections) == 1
                    assert collections[0].name == "test-recipes"

                    # Second call - should use cache
                    collections = self.mod.list_collections(registry_name="test-reg")
                    assert call_count[0] == 1  # No additional network calls
                    assert len(collections) == 1

    def test_cache_invalidation_after_clear(self):
        """Cache invalidation forces fresh fetch."""
        mock_index = {
            "annotations": {
                "name": "test-recipes",
                "version": "1.0.0",
                "description": "Test",
                "vendor": "Test",
                "license": "MIT",
            },
            "manifests": [{"digest": "sha256:abc", "size": 100}],
            "digest": "sha256:manifest-digest",
        }

        call_count = [0]

        def mock_fetch_index(url, tag, auth=None):
            call_count[0] += 1
            return mock_index

        def mock_list_tags(url, auth=None):
            return ["sha256:abc123"]

        with patch.object(self.mod, "_oras_list_tags", side_effect=mock_list_tags):
            with patch.object(
                self.mod, "_fetch_oci_index", side_effect=mock_fetch_index
            ):
                with patch.object(
                    self.mod,
                    "_load_registries",
                    return_value=[
                        {"name": "test-reg", "url": "example.com/repo", "enabled": True}
                    ],
                ):
                    # First call - cache miss
                    self.mod.list_collections(registry_name="test-reg")
                    assert call_count[0] == 1

                    # Second call - cache hit
                    self.mod.list_collections(registry_name="test-reg")
                    assert call_count[0] == 1

                    # Clear cache
                    self.mod.clear_oci_cache()

                    # Third call - cache miss again
                    self.mod.list_collections(registry_name="test-reg")
                    assert call_count[0] == 2
