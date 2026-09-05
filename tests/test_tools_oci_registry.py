"""Tests for OCI registry tools module."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import yaml

from spark_pulse.tools.oci_registry import (
    BUNDLED_REGISTRIES_CONFIG,
    _safe_layer_filename,
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


def bundled_registries() -> list[dict]:
    """The registry list the package ships as its default."""
    return yaml.safe_load(BUNDLED_REGISTRIES_CONFIG.read_text())["registries"]


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

    def test_bundled_default_ships_inside_the_package(self):
        """The fallback path resolves to the file the wheel actually ships.

        It lives at ``spark_pulse/registries.yaml``, not under ``tools/``; a
        fallback pointing anywhere else leaves a fresh install with no
        registries at all.
        """
        assert BUNDLED_REGISTRIES_CONFIG.parent.name == "spark_pulse"
        assert BUNDLED_REGISTRIES_CONFIG.exists()
        assert bundled_registries()

    def test_missing_user_config_falls_back_to_the_bundled_default(
        self, temp_registries_file
    ):
        """A user who has never written a config still sees the shipped registries."""
        assert not temp_registries_file.exists()
        assert _load_registries() == bundled_registries()

    def test_load_empty_registries(self, temp_registries_file, tmp_path, monkeypatch):
        """With neither a user config nor a bundled default the list is empty."""
        import spark_pulse.tools.oci_registry as mod

        monkeypatch.setattr(
            mod, "BUNDLED_REGISTRIES_CONFIG", tmp_path / "nowhere" / "registries.yaml"
        )
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
        # The first write materialises the bundled defaults into the user
        # config alongside the addition, so neither is lost.
        loaded = _load_registries()
        assert [r["name"] for r in loaded] == [
            r["name"] for r in bundled_registries()
        ] + ["new-registry"]

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
        """An explicitly empty registry list yields nothing and hits no network."""
        _save_registries([])
        with patch("spark_pulse.tools.oci_registry._oras_list_tags") as mock_tags:
            collections = list_collections()
        assert collections == []
        mock_tags.assert_not_called()

    @patch("spark_pulse.tools.oci_registry._oras_list_tags")
    @patch("spark_pulse.tools.oci_registry._fetch_oci_index")
    def test_list_collections_with_registries(
        self, mock_index, mock_tags, sample_registry
    ):
        """Listing collections from a registry returns parsed data.

        Note: Deduplication keeps only the latest version for each (name, registry) pair.
        """
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
        # Deduplication keeps only the latest version (1.1.0)
        assert len(collections) == 1
        assert collections[0].name == "test-recipes"
        assert collections[0].version == "1.1.0"
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
        """Installing with an explicitly empty registry list raises ValueError."""
        _save_registries([])
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
                # When no updates, returns updated=0
                assert result.get("updated") == 0 or result.get("skipped")


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


class TestLayerFilenameIsNotRegistryControlled:
    """A layer's title comes from a remote registry, so it is not a filename.

    ``org.opencontainers.image.title`` was joined onto the layout directory
    verbatim. A registry that served ``../../../.ssh/authorized_keys`` as a
    layer title therefore wrote that file, as whichever user runs the server,
    on any ``oci install``. These are the payloads that must not survive.
    """

    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../.ssh/authorized_keys",
            "../../etc/cron.d/pwn",
            "/etc/passwd",
            "..",
            ".",
            "",
            "   ",
            "sub/dir/recipe.yaml",
            "..\\..\\windows\\system32\\drivers\\etc\\hosts",
        ],
    )
    def test_a_hostile_title_never_escapes_the_layout_directory(
        self, hostile, tmp_path
    ):
        name = _safe_layer_filename(hostile, "sha256:abcdef1234567890")

        assert "/" not in name and "\\" not in name
        assert name not in {"", ".", ".."}
        # The real test: joining the result must stay inside the directory.
        resolved = (tmp_path / name).resolve()
        assert (
            resolved.parent == tmp_path.resolve()
        ), f"{hostile!r} escaped to {resolved}"

    def test_an_ordinary_title_is_kept_as_it_is(self, tmp_path):
        assert _safe_layer_filename("qwen3-8b.yaml", "sha256:abc") == "qwen3-8b.yaml"

    def test_a_discarded_title_falls_back_to_the_digest(self):
        # Deterministic, and distinct per layer, so two hostile layers in one
        # artifact cannot collide onto a single file.
        assert _safe_layer_filename("../..", "sha256:deadbeefcafe") == (
            "recipe-sha256:deadb.yaml"
        )


class TestOrasPullToLayout:
    """Tests for _oras_pull_to_layout — OCI index traversal.

    Regression tests for the fix that handles OCI artifacts with index
    structure (index → recipe manifests → YAML layers) instead of flat layers.
    """

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_pull_traverses_index_structure(self, mock_client_class, tmp_path):
        """Pulling from index artifact downloads YAML layers from recipe manifests."""
        from spark_pulse.tools.oci_registry import _oras_pull_to_layout

        mock_client = mock_client_class.return_value

        # Simulate OCI index manifest (tagged)
        def get_manifest_side_effect(target):
            if target.endswith(":1.0.0"):
                # Index manifest
                return {
                    "manifests": [
                        {
                            "digest": "sha256:recipe1",
                            "annotations": {"name": "TestRecipe1"},
                        },
                        {
                            "digest": "sha256:recipe2",
                            "annotations": {"name": "TestRecipe2"},
                        },
                    ]
                }
            elif "sha256:recipe1" in target:
                return {
                    "layers": [
                        {
                            "digest": "sha256:yaml1",
                            "size": 100,
                            "annotations": {
                                "org.opencontainers.image.title": "TestRecipe1.yaml"
                            },
                        }
                    ]
                }
            elif "sha256:recipe2" in target:
                return {
                    "layers": [
                        {
                            "digest": "sha256:yaml2",
                            "size": 200,
                            "annotations": {
                                "org.opencontainers.image.title": "TestRecipe2.yaml"
                            },
                        }
                    ]
                }
            return {}

        mock_client.get_manifest.side_effect = get_manifest_side_effect

        # Make download_blob actually write files
        def download_blob_side_effect(target, digest, outfile):
            Path(outfile).write_text(f"# Recipe content for {Path(outfile).name}")

        mock_client.download_blob.side_effect = download_blob_side_effect

        layout_dir = tmp_path / "layout"
        _oras_pull_to_layout("ghcr.io/test/repo", "1.0.0", layout_dir)

        # Should have downloaded 2 YAML files
        yaml_files = sorted(layout_dir.glob("*.yaml"))
        assert len(yaml_files) == 2
        assert yaml_files[0].name == "TestRecipe1.yaml"
        assert yaml_files[1].name == "TestRecipe2.yaml"

        # Verify download_blob was called for each layer
        assert mock_client.download_blob.call_count == 2

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_pull_uses_recipe_name_when_no_title(self, mock_client_class, tmp_path):
        """Falls back to recipe name annotation when title is missing."""
        from spark_pulse.tools.oci_registry import _oras_pull_to_layout

        mock_client = mock_client_class.return_value

        def get_manifest_side_effect(target):
            if target.endswith(":1.0.0"):
                return {
                    "manifests": [
                        {"digest": "sha256:r1", "annotations": {"name": "MyRecipe"}}
                    ]
                }
            return {
                "layers": [
                    {
                        "digest": "sha256:y1",
                        "size": 50,
                        "annotations": {"name": "MyRecipe"},  # name on layer, not title
                    }
                ]
            }

        mock_client.get_manifest.side_effect = get_manifest_side_effect

        def download_blob_side_effect(target, digest, outfile):
            Path(outfile).write_text("# Recipe content")

        mock_client.download_blob.side_effect = download_blob_side_effect

        layout_dir = tmp_path / "layout"
        _oras_pull_to_layout("ghcr.io/test/repo", "1.0.0", layout_dir)

        yaml_files = list(layout_dir.glob("*.yaml"))
        assert len(yaml_files) == 1
        assert yaml_files[0].name == "MyRecipe.yaml"

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_pull_handles_empty_index(self, mock_client_class, tmp_path):
        """Empty index produces no files."""
        from spark_pulse.tools.oci_registry import _oras_pull_to_layout

        mock_client = mock_client_class.return_value
        mock_client.get_manifest.return_value = {"manifests": []}

        layout_dir = tmp_path / "layout"
        _oras_pull_to_layout("ghcr.io/test/repo", "1.0.0", layout_dir)

        yaml_files = list(layout_dir.glob("*.yaml"))
        assert len(yaml_files) == 0

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_pull_handles_recipe_fetch_failure(self, mock_client_class, tmp_path):
        """Continues when individual recipe manifest fetch fails."""
        from spark_pulse.tools.oci_registry import _oras_pull_to_layout

        mock_client = mock_client_class.return_value

        call_count = [0]

        def get_manifest_side_effect(target):
            call_count[0] += 1
            if call_count[0] == 1:
                # Index with 2 recipes
                return {
                    "manifests": [
                        {
                            "digest": "sha256:good",
                            "annotations": {"name": "GoodRecipe"},
                        },
                        {"digest": "sha256:bad", "annotations": {"name": "BadRecipe"}},
                    ]
                }
            elif "sha256:good" in target:
                return {
                    "layers": [
                        {
                            "digest": "sha256:y1",
                            "size": 50,
                            "annotations": {"name": "GoodRecipe"},
                        }
                    ]
                }
            else:
                raise ValueError("Not Found")

        mock_client.get_manifest.side_effect = get_manifest_side_effect

        def download_blob_side_effect(target, digest, outfile):
            Path(outfile).write_text("# Good recipe content")

        mock_client.download_blob.side_effect = download_blob_side_effect

        layout_dir = tmp_path / "layout"
        _oras_pull_to_layout("ghcr.io/test/repo", "1.0.0", layout_dir)

        # Should have the good recipe despite the bad one failing
        yaml_files = list(layout_dir.glob("*.yaml"))
        assert len(yaml_files) == 1
        assert yaml_files[0].name == "GoodRecipe.yaml"

    @patch("spark_pulse.tools.oci_registry._oras_client")
    def test_pull_handles_layer_download_failure(self, mock_client_class, tmp_path):
        """Continues when individual layer download fails."""
        from spark_pulse.tools.oci_registry import _oras_pull_to_layout

        mock_client = mock_client_class.return_value

        def get_manifest_side_effect(target):
            if target.endswith(":1.0.0"):
                return {
                    "manifests": [
                        {"digest": "sha256:r1", "annotations": {"name": "Recipe1"}},
                        {"digest": "sha256:r2", "annotations": {"name": "Recipe2"}},
                    ]
                }
            elif "sha256:r1" in target:
                return {
                    "layers": [
                        {
                            "digest": "sha256:y1",
                            "size": 50,
                            "annotations": {"name": "Recipe1"},
                        }
                    ]
                }
            else:
                return {
                    "layers": [
                        {
                            "digest": "sha256:y2",
                            "size": 50,
                            "annotations": {"name": "Recipe2"},
                        }
                    ]
                }

        mock_client.get_manifest.side_effect = get_manifest_side_effect

        call_count = [0]

        def download_blob_side_effect(target, digest, outfile):
            call_count[0] += 1
            if call_count[0] == 1:
                Path(outfile).write_text("# Recipe1 content")
            else:
                raise OSError("Download failed")

        mock_client.download_blob.side_effect = download_blob_side_effect

        layout_dir = tmp_path / "layout"
        _oras_pull_to_layout("ghcr.io/test/repo", "1.0.0", layout_dir)

        yaml_files = list(layout_dir.glob("*.yaml"))
        assert len(yaml_files) == 1
        assert yaml_files[0].name == "Recipe1.yaml"


class TestExtractRecipesFromLayout:
    """Tests for _extract_recipes_from_layout — YAML file discovery.

    Regression tests for the fix that scans for YAML files in flat directory
    instead of expecting OCI layout structure (index.json + blobs/).
    """

    def test_extract_finds_yaml_files(self, tmp_path):
        """Extracts all .yaml and .yml files from layout directory."""
        from spark_pulse.tools.oci_registry import _extract_recipes_from_layout

        layout_dir = tmp_path / "layout"
        layout_dir.mkdir()

        # Create test YAML files
        (layout_dir / "recipe1.yaml").write_text("name: Recipe1\ntensor_parallel: 2")
        (layout_dir / "recipe2.yml").write_text("name: Recipe2\ntensor_parallel: 4")
        (layout_dir / "readme.txt").write_text("not a recipe")

        extract_dir = tmp_path / "extracted"
        recipes = _extract_recipes_from_layout(layout_dir, extract_dir)

        assert len(recipes) == 2
        filenames = {r["filename"] for r in recipes}
        assert "recipe1.yaml" in filenames
        assert "recipe2.yml" in filenames

    def test_extract_returns_correct_content_and_digest(self, tmp_path):
        """Extracted recipes have correct content and SHA256 digest."""
        import hashlib

        from spark_pulse.tools.oci_registry import _extract_recipes_from_layout

        layout_dir = tmp_path / "layout"
        layout_dir.mkdir()

        content = "name: TestRecipe\nmodel: test-model"
        (layout_dir / "TestRecipe.yaml").write_text(content)

        extract_dir = tmp_path / "extracted"
        recipes = _extract_recipes_from_layout(layout_dir, extract_dir)

        assert len(recipes) == 1
        assert recipes[0]["content"] == content
        assert recipes[0]["filename"] == "TestRecipe.yaml"
        assert recipes[0]["size"] == len(content.encode())
        assert (
            recipes[0]["digest"]
            == f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"
        )

    def test_extract_empty_directory(self, tmp_path):
        """Empty layout directory returns empty list."""
        from spark_pulse.tools.oci_registry import _extract_recipes_from_layout

        layout_dir = tmp_path / "layout"
        layout_dir.mkdir()

        extract_dir = tmp_path / "extracted"
        recipes = _extract_recipes_from_layout(layout_dir, extract_dir)

        assert recipes == []

    def test_extract_no_yaml_files(self, tmp_path):
        """Directory with non-YAML files returns empty list."""
        from spark_pulse.tools.oci_registry import _extract_recipes_from_layout

        layout_dir = tmp_path / "layout"
        layout_dir.mkdir()

        (layout_dir / "data.json").write_text('{"key": "value"}')
        (layout_dir / "script.py").write_text("print('hello')")

        extract_dir = tmp_path / "extracted"
        recipes = _extract_recipes_from_layout(layout_dir, extract_dir)

        assert recipes == []

    def test_extract_skips_unreadable_files(self, tmp_path):
        """Unreadable YAML files are logged as warnings, not errors."""
        from spark_pulse.tools.oci_registry import _extract_recipes_from_layout

        layout_dir = tmp_path / "layout"
        layout_dir.mkdir()

        # Create a valid and an invalid YAML file
        (layout_dir / "good.yaml").write_text("name: Good")
        bad_file = layout_dir / "bad.yaml"
        bad_file.write_text("name: Bad")
        bad_file.chmod(0o000)  # Make unreadable

        extract_dir = tmp_path / "extracted"
        recipes = _extract_recipes_from_layout(layout_dir, extract_dir)

        # Should only get the good recipe
        assert len(recipes) == 1
        assert recipes[0]["filename"] == "good.yaml"

        # Restore permissions for test cleanup
        bad_file.chmod(0o644)
