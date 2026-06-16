"""Tests for OCI registry tools module."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spark_pulse.tools.oci_registry import (
    add_registry,
    apply_updates,
    check_updates,
    get_default_registry,
    get_registry,
    install_collection,
    list_collections,
    list_registries,
    remove_registry,
    run_auto_update,
    update_registry,
    _auth_headers,
    _load_registries,
    _oras_client,
    _oras_list_tags,
    _save_registries,
    _write_recipe_meta,
    _read_recipe_meta,
)


@pytest.fixture
def temp_registries_file(tmp_path, monkeypatch):
    """Create a temporary registries.yaml file."""
    test_dir = tmp_path / ".config" / "spark-pulse"
    test_dir.mkdir(parents=True)
    reg_file = test_dir / "registries.yaml"
    monkeypatch.setenv("HOME", str(tmp_path))
    # Re-import to pick up new HOME
    import spark_pulse.tools.oci_registry as mod

    mod.REGISTRIES_CONFIG = reg_file
    mod.OCI_CACHE_DIR = tmp_path / ".cache" / "spark-pulse" / "oci"
    mod.RECIPES_DIR = test_dir / "recipes"
    return reg_file


@pytest.fixture
def sample_registry(temp_registries_file):
    """Create a sample registry config."""
    reg = {
        "name": "test-registry",
        "url": "ghcr.io/test/test-recipes",
        "enabled": True,
        "default": True,
        "auth": {"type": "token", "token": "test-token"},
    }
    _save_registries([reg])
    return reg


class TestRegistryConfig:
    """Tests for registry configuration management."""

    def test_load_empty_registries(self, temp_registries_file):
        """Loading from non-existent file returns empty list."""
        assert _load_registries() == []

    def test_load_registries(self, sample_registry):
        """Loading registries returns the configured list."""
        regs = _load_registries()
        assert len(regs) == 1
        assert regs[0]["name"] == "test-registry"
        assert regs[0]["url"] == "ghcr.io/test/test-recipes"

    def test_add_registry(self, temp_registries_file):
        """Adding a registry persists it."""
        reg = {
            "name": "new-registry",
            "url": "example.com/recipes",
            "enabled": True,
            "default": False,
            "auth": {},
        }
        result = add_registry(reg)
        assert result["name"] == "new-registry"
        assert result["url"] == "example.com/recipes"
        assert len(_load_registries()) == 1

    def test_add_duplicate_registry(self, sample_registry):
        """Adding a registry with existing name replaces it."""
        reg = {
            "name": "test-registry",
            "url": "new-url.example.com",
            "enabled": False,
            "default": False,
            "auth": {},
        }
        result = add_registry(reg)
        assert result["url"] == "new-url.example.com"
        assert result["enabled"] is False
        assert len(_load_registries()) == 1

    def test_remove_registry(self, sample_registry):
        """Removing a registry deletes it."""
        assert remove_registry("test-registry") is True
        assert len(_load_registries()) == 0

    def test_remove_nonexistent_registry(self, sample_registry):
        """Removing a non-existent registry returns False."""
        assert remove_registry("nonexistent") is False

    def test_update_registry(self, sample_registry):
        """Updating a registry modifies its fields."""
        result = update_registry("test-registry", {"enabled": False})
        assert result is not None
        assert result["enabled"] is False

    def test_update_nonexistent_registry(self, sample_registry):
        """Updating a non-existent registry returns None."""
        assert update_registry("nonexistent", {"enabled": True}) is None

    def test_get_registry(self, sample_registry):
        """Getting a registry by name returns it."""
        reg = get_registry("test-registry")
        assert reg is not None
        assert reg["name"] == "test-registry"

    def test_get_default_registry(self, sample_registry):
        """Getting the default registry returns the marked one."""
        reg = get_default_registry()
        assert reg is not None
        assert reg["name"] == "test-registry"

    def test_list_registries(self, sample_registry):
        """Listing registries includes connectivity status."""
        with patch("spark_pulse.tools.oci_registry._oras_list_tags") as mock_tags:
            mock_tags.side_effect = Exception("Connection failed")
            regs = list_registries()
            assert len(regs) == 1
            assert regs[0]["connected"] is False


class TestRecipeMetadata:
    """Tests for recipe metadata management."""

    def test_write_and_read_recipe_meta(self, tmp_path, monkeypatch):
        """Writing and reading recipe metadata works."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import spark_pulse.tools.oci_registry as mod

        mod.RECIPES_DIR = tmp_path / "recipes"
        mod.RECIPES_DIR.mkdir(parents=True)

        _write_recipe_meta(
            recipe_filename="test-recipe.yaml",
            source="test-registry",
            collection="test-coll",
            version="1.0.0",
            digest="sha256:abc123",
        )

        meta = _read_recipe_meta("test-recipe.yaml")
        assert meta is not None
        assert meta.source == "test-registry"
        assert meta.collection == "test-coll"
        assert meta.version == "1.0.0"
        assert meta.digest == "sha256:abc123"

    def test_read_nonexistent_meta(self, tmp_path, monkeypatch):
        """Reading non-existent metadata returns None."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import spark_pulse.tools.oci_registry as mod

        mod.RECIPES_DIR = tmp_path / "recipes"
        mod.RECIPES_DIR.mkdir(parents=True)

        assert _read_recipe_meta("nonexistent.yaml") is None


class TestListCollections:
    """Tests for collection listing."""

    def test_list_collections_no_registries(self, temp_registries_file):
        """Listing with no registries returns empty."""
        collections = list_collections()
        assert collections == []

    @patch("spark_pulse.tools.oci_registry._oras_list_tags")
    @patch("spark_pulse.tools.oci_registry._fetch_oci_index")
    def test_list_collections_with_registries(
        self, mock_index, mock_tags, sample_registry
    ):
        """Listing collections from a registry returns parsed data."""
        mock_tags.return_value = ["1.0.0", "1.1.0"]
        mock_index.return_value = {
            "annotations": {
                "name": "test-recipes",
                "version": "1.0.0",
                "description": "Test recipes",
                "vendor": "TestVendor",
                "license": "MIT",
            },
            "manifests": [
                {"digest": "sha256:abc"},
                {"digest": "sha256:def"},
            ],
        }

        collections = list_collections(registry_name="test-registry")
        assert len(collections) == 2
        assert collections[0].name == "test-recipes"
        assert collections[0].recipe_count == 2

    def test_list_collections_filter_by_registry(self, sample_registry):
        """Filtering by non-existent registry raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            list_collections(registry_name="nonexistent")


class TestInstallCollection:
    """Tests for collection installation."""

    @patch("spark_pulse.tools.oci_registry.get_registry")
    @patch("spark_pulse.tools.oci_registry.list_collections")
    @patch("spark_pulse.tools.oci_registry._pull_oci_to_layout")
    @patch("spark_pulse.tools.oci_registry._extract_recipes_from_layout")
    def test_install_collection_success(
        self,
        mock_extract,
        mock_pull,
        mock_collections,
        mock_get_reg,
        tmp_path,
        monkeypatch,
    ):
        """Successful installation returns installed filenames."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import spark_pulse.tools.oci_registry as mod

        mod.RECIPES_DIR = tmp_path / "recipes"

        mock_get_reg.return_value = {
            "name": "test-registry",
            "url": "test.com",
            "auth": {},
        }
        mock_collections.return_value = [
            SimpleNamespace(name="test-coll", version="1.0.0", recipe_count=2)
        ]
        mock_extract.return_value = [
            {
                "filename": "recipe1.yaml",
                "content": "test: 1",
                "digest": "sha256:abc",
                "size": 10,
            },
            {
                "filename": "recipe2.yaml",
                "content": "test: 2",
                "digest": "sha256:def",
                "size": 10,
            },
        ]

        installed = install_collection(
            name="test-coll",
            version="1.0.0",
            registry_name="test-registry",
        )

        assert len(installed) == 2
        assert "recipe1.yaml" in installed
        assert "recipe2.yaml" in installed

    def test_install_collection_no_registries(self, temp_registries_file):
        """Installing with no registries raises ValueError."""
        with pytest.raises(ValueError, match="No registries configured"):
            install_collection(name="test", version="1.0.0")

    def test_install_collection_dry_run(self, sample_registry, tmp_path, monkeypatch):
        """Dry run returns empty list without installing."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import spark_pulse.tools.oci_registry as mod

        mod.RECIPES_DIR = tmp_path / "recipes"

        with patch("spark_pulse.tools.oci_registry.get_registry") as mock_get_reg:
            mock_get_reg.return_value = {
                "name": "test-registry",
                "url": "test.com",
                "auth": {},
            }
            with patch("spark_pulse.tools.oci_registry.list_collections") as mock_cols:
                mock_cols.return_value = [SimpleNamespace(name="test", version="1.0.0")]
                with patch("spark_pulse.tools.oci_registry._pull_oci_to_layout"):
                    with patch(
                        "spark_pulse.tools.oci_registry._extract_recipes_from_layout"
                    ):
                        installed = install_collection(
                            name="test",
                            version="1.0.0",
                            registry_name="test-registry",
                            dry_run=True,
                        )
                        assert installed == []


class TestCheckUpdates:
    """Tests for update checking."""

    def test_check_updates_no_oci_recipes(self, tmp_path, monkeypatch):
        """Checking updates with no OCI recipes returns empty."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import spark_pulse.tools.oci_registry as mod

        mod.RECIPES_DIR = tmp_path / "recipes"
        mod.RECIPES_DIR.mkdir(parents=True)

        updates = check_updates()
        assert updates == []


class TestApplyUpdates:
    """Tests for update application."""

    def test_apply_updates(self, sample_registry, tmp_path, monkeypatch):
        """Applying updates calls install_collection for each."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import spark_pulse.tools.oci_registry as mod

        mod.RECIPES_DIR = tmp_path / "recipes"

        with patch("spark_pulse.tools.oci_registry.install_collection") as mock_install:
            mock_install.return_value = ["recipe1.yaml"]
            results = apply_updates(
                [
                    {
                        "collection": "test",
                        "target_version": "2.0.0",
                        "registry": "test",
                    },
                ]
            )
            assert len(results) == 1
            assert results[0]["success"] is True
            assert results[0]["installed"] == ["recipe1.yaml"]

    def test_apply_updates_failure(self, sample_registry):
        """Failed updates return error info."""
        with patch("spark_pulse.tools.oci_registry.install_collection") as mock_install:
            mock_install.side_effect = ValueError("Collection not found")
            results = apply_updates(
                [
                    {
                        "collection": "test",
                        "target_version": "2.0.0",
                        "registry": "test",
                    },
                ]
            )
            assert len(results) == 1
            assert results[0]["success"] is False
            assert "not found" in results[0]["error"]


class TestAutoUpdate:
    """Tests for auto-update functionality."""

    def test_auto_update_disabled(self, tmp_path, monkeypatch):
        """Auto-update returns skipped when disabled."""
        monkeypatch.setenv("HOME", str(tmp_path))

        with patch("spark_pulse.tools.oci_registry.config") as mock_config:
            mock_config.oci_auto_update_enabled = False
            result = run_auto_update()
            assert result.get("skipped") is True

    def test_auto_update_no_updates(self, sample_registry, tmp_path, monkeypatch):
        """Auto-update returns 0 updated when no updates available."""
        monkeypatch.setenv("HOME", str(tmp_path))
        import spark_pulse.tools.oci_registry as mod

        mod.RECIPES_DIR = tmp_path / "recipes"
        mod.RECIPES_DIR.mkdir(parents=True)

        with patch("spark_pulse.config.config") as mock_config:
            mock_config.oci_auto_update_enabled = True
            with patch("spark_pulse.tools.oci_registry.check_updates") as mock_check:
                mock_check.return_value = []
                result = run_auto_update()
                assert result.get("updated") == 0


class TestAuthHeaders:
    """Tests for auth header generation."""

    def test_auth_headers_none(self):
        """No auth returns empty headers."""
        assert _auth_headers(None) == {}

    def test_auth_headers_empty_dict(self):
        """Empty auth dict returns empty headers."""
        assert _auth_headers({}) == {}

    def test_auth_headers_token(self):
        """Token auth returns Bearer header."""
        result = _auth_headers({"type": "token", "token": "my-token"})
        assert result == {"Authorization": "Bearer my-token"}

    def test_auth_headers_username_password(self):
        """Username/password auth returns Basic header."""
        import base64

        result = _auth_headers(
            {"type": "username_password", "username": "user", "password": "pass"}
        )
        expected_creds = base64.b64encode(b"user:pass").decode()
        assert result == {"Authorization": f"Basic {expected_creds}"}

    def test_auth_headers_username_password_env(self, monkeypatch):
        """Password env var is resolved."""
        import base64

        monkeypatch.setenv("MY_PASSWORD", "env-pass")
        result = _auth_headers(
            {
                "type": "username_password",
                "username": "user",
                "password_env": "MY_PASSWORD",
            }
        )
        expected_creds = base64.b64encode(b"user:env-pass").decode()
        assert result == {"Authorization": f"Basic {expected_creds}"}


class TestOrasClient:
    """Tests for oras client creation."""

    def test_oras_client_no_auth(self):
        """Client without auth has no Authorization header."""
        client = _oras_client()
        assert "Authorization" not in client.session.headers

    def test_oras_client_with_token_auth(self):
        """Client with token auth has Authorization header."""
        client = _oras_client(auth={"type": "token", "token": "my-token"})
        assert client.session.headers.get("Authorization") == "Bearer my-token"

    def test_oras_client_with_username_password_auth(self):
        """Client with username/password auth has Authorization header."""
        import base64

        auth = {"type": "username_password", "username": "user", "password": "pass"}
        client = _oras_client(auth=auth)
        expected_creds = base64.b64encode(b"user:pass").decode()
        assert client.session.headers.get("Authorization") == f"Basic {expected_creds}"


class TestOrasListTags:
    """Tests for oras list tags function."""

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_list_tags_public_registry(self, mock_client_class):
        """Public registry (no auth) lists tags correctly."""
        mock_client = mock_client_class.return_value
        mock_client.get_tags.return_value = ["1.0.0", "latest"]

        tags = _oras_list_tags("ghcr.io/test/repo")

        assert tags == ["1.0.0", "latest"]
        mock_client_class.assert_called_once_with(None)
        mock_client.get_tags.assert_called_once_with("ghcr.io/test/repo")

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_list_tags_auth_registry(self, mock_client_class):
        """Authenticated registry passes auth to client."""
        mock_client = mock_client_class.return_value
        mock_client.get_tags.return_value = ["1.0.0"]

        auth = {"type": "token", "token": "secret"}
        tags = _oras_list_tags("ghcr.io/test/repo", auth=auth)

        assert tags == ["1.0.0"]
        mock_client_class.assert_called_once_with(auth)

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_list_tags_empty_response(self, mock_client_class):
        """Empty tag list returns empty list."""
        mock_client = mock_client_class.return_value
        mock_client.get_tags.return_value = []

        tags = _oras_list_tags("ghcr.io/test/repo")
        assert tags == []

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_list_tags_none_response(self, mock_client_class):
        """None response returns empty list."""
        mock_client = mock_client_class.return_value
        mock_client.get_tags.return_value = None

        tags = _oras_list_tags("ghcr.io/test/repo")
        assert tags == []
