"""Behavioural tests for :mod:`spark_pulse.tools.oci_registry`.

These complement ``tests/test_tools_oci_registry.py`` (registry CRUD, tag
listing, layout extraction) and ``tests/test_oci_caching.py`` (cache TTL,
background updater) by exercising the parts that talk to a registry that
misbehaves: malformed configs and manifests, auth handling, hostile layer
annotations, partial failures, the install/update/uninstall lifecycle and the
auto-update driver.

Everything is mocked at the ``oras`` client boundary — no network, no
subprocesses, no sleeps.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

# NB: the ``import spark_pulse.tools.oci_registry`` form is deliberate — it
# imports the REAL module even under SIMULATION_MODE=1 (see CLAUDE.md).
import spark_pulse.tools.oci_registry as oci

#: The "file we cannot read" paths below rely on chmod actually denying access.
needs_unprivileged = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions",
)


# ── Fakes for the oras client boundary ───────────────────────────────────────


class FakeResponse:
    """Minimal stand-in for the requests.Response returned by ``get_blob``."""

    def __init__(self, text: str, error: Exception | None = None):
        self.text = text
        self._error = error

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error


class FakeRegistry:
    """A canned OCI registry: tags, manifests by tag/digest, and blobs."""

    def __init__(
        self,
        tags: list[str] | None = None,
        index_by_tag: dict | None = None,
        manifest_by_digest: dict | None = None,
        blobs: dict | None = None,
        tags_error: Exception | None = None,
    ):
        self.tags = tags or []
        self.index_by_tag = index_by_tag or {}
        self.manifest_by_digest = manifest_by_digest or {}
        self.blobs = blobs or {}
        self.tags_error = tags_error
        self.auth_seen: list[dict | None] = []
        self.manifest_refs: list[str] = []
        self.blob_error: Exception | None = None


class FakeClient:
    def __init__(self, registry: FakeRegistry, auth: dict | None = None):
        self.registry = registry
        self.session = SimpleNamespace(headers={})
        registry.auth_seen.append(auth)

    # -- oras API surface used by the module --------------------------------
    def get_tags(self, url):
        if self.registry.tags_error:
            raise self.registry.tags_error
        return self.registry.tags

    def get_manifest(self, ref):
        self.registry.manifest_refs.append(ref)
        if "@" in ref:
            digest = ref.split("@", 1)[1]
            if digest not in self.registry.manifest_by_digest:
                raise RuntimeError(f"404 manifest {digest}")
            return self.registry.manifest_by_digest[digest]
        tag = ref.rsplit(":", 1)[1]
        if tag not in self.registry.index_by_tag:
            raise RuntimeError(f"404 index {tag}")
        return self.registry.index_by_tag[tag]

    def download_blob(self, ref, digest, outfile):
        if digest not in self.registry.blobs:
            raise RuntimeError(f"404 blob {digest}")
        Path(outfile).write_text(self.registry.blobs[digest])

    def get_blob(self, url, digest):
        if self.registry.blob_error:
            raise self.registry.blob_error
        return FakeResponse(self.registry.blobs.get(digest, ""))


@pytest.fixture
def fake_client(monkeypatch):
    """Patch ``_oras_client`` so every registry call hits a canned registry."""
    registry = FakeRegistry()

    def factory(auth=None):
        return FakeClient(registry, auth)

    monkeypatch.setattr(oci, "_oras_client", factory)
    return registry


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point every module-level path at ``tmp_path`` (restored afterwards)."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    recipes = cfg / "recipes"
    recipes.mkdir()
    cache = tmp_path / "cache" / "oci"
    monkeypatch.setattr(oci, "REGISTRIES_CONFIG", cfg / "registries.yaml")
    monkeypatch.setattr(oci, "RECIPES_DIR", recipes)
    monkeypatch.setattr(oci, "OCI_CACHE_DIR", cache)
    monkeypatch.setattr(oci, "OCI_META_CACHE_DIR", cache / "meta_cache")
    monkeypatch.setenv("OCI_CACHE_TTL_SECONDS", "300")
    # Always write a real (possibly empty) user config so behaviour never
    # depends on whether a bundled default happens to be readable.
    oci._save_registries([])
    return SimpleNamespace(
        registries_file=cfg / "registries.yaml",
        recipes_dir=recipes,
        cache_dir=cache,
        tmp=tmp_path,
    )


def _registry(name="reg", url="ghcr.io/test/recipes", **extra):
    reg = {"name": name, "url": url, "enabled": True, "default": False, "auth": {}}
    reg.update(extra)
    return reg


def _index(name="pack", tag_version="1.0.0", manifests=None, **annotations):
    ann = {
        "name": name,
        "version": tag_version,
        "description": "desc",
        "vendor": "acme",
        "license": "MIT",
    }
    ann.update(annotations)
    return {"annotations": ann, "manifests": manifests or []}


# ── Cache internals ──────────────────────────────────────────────────────────


class TestCacheInternals:
    def test_cache_ttl_falls_back_when_env_is_not_a_number(self, monkeypatch):
        monkeypatch.setenv("OCI_CACHE_TTL_SECONDS", "soon-ish")
        assert oci._cache_ttl() == oci._DEFAULT_CACHE_TTL

    def test_background_interval_falls_back_when_env_is_not_a_number(self, monkeypatch):
        monkeypatch.setenv("OCI_BACKGROUND_CHECK_INTERVAL_SECONDS", "hourly")
        assert oci._background_check_interval() == 900

    def test_read_cache_returns_none_for_corrupt_payload(self, env):
        path = oci._cache_path("reg:1.0.0")
        path.write_text("{not json")
        assert oci._read_cache("reg:1.0.0") is None
        # A corrupt file is tolerated, not deleted — the next write replaces it.
        assert path.exists()

    def test_write_cache_swallows_io_errors(self, env, monkeypatch):
        monkeypatch.setattr(
            oci, "_cache_path", lambda key: (_ for _ in ()).throw(OSError("read-only"))
        )
        oci._write_cache("reg:1.0.0", {"a": 1})  # must not raise

    def test_clear_cache_without_a_directory_is_a_noop(self, env):
        assert not oci.OCI_META_CACHE_DIR.exists()
        oci._clear_cache()  # must not raise
        oci._clear_cache("reg:1.0.0")

    def test_clear_cache_removes_one_key_or_everything(self, env):
        oci._write_cache("reg:1.0.0", {"a": 1})
        oci._write_cache("reg:2.0.0", {"a": 2})

        oci._clear_cache("reg:1.0.0")
        assert oci._read_cache("reg:1.0.0") is None
        assert oci._read_cache("reg:2.0.0") == {"a": 2}

        oci._clear_cache()
        assert list(oci.OCI_META_CACHE_DIR.glob("*.json")) == []

    def test_cache_key_is_sanitised_into_a_flat_filename(self, env):
        path = oci._cache_path(oci._cache_key("ghcr.io/org/repo", "sha256:abc"))
        assert path.parent == oci.OCI_META_CACHE_DIR
        assert "/" not in path.name and ":" not in path.name


# ── Registry configuration ───────────────────────────────────────────────────


class TestLoadRegistries:
    def test_yaml_that_is_not_a_mapping_yields_no_registries(self, env):
        env.registries_file.write_text("- just\n- a\n- list\n")
        assert oci._load_registries() == []

    def test_mapping_without_registries_key_yields_no_registries(self, env):
        env.registries_file.write_text("other: {}\n")
        assert oci._load_registries() == []

    def test_registries_key_that_is_not_a_list_yields_no_registries(self, env):
        env.registries_file.write_text("registries:\n  name: single\n")
        assert oci._load_registries() == []

    def test_unparseable_yaml_is_logged_and_yields_no_registries(self, env, caplog):
        env.registries_file.write_text("registries: [oops\n")
        with caplog.at_level("WARNING"):
            assert oci._load_registries() == []
        assert "Failed to load registries config" in caplog.text


class TestRegistryLookup:
    def test_get_registry_returns_none_when_unknown(self, env):
        oci._save_registries([_registry("a")])
        assert oci.get_registry("missing") is None

    def test_default_registry_prefers_the_default_flag(self, env):
        oci._save_registries(
            [_registry("first"), _registry("second", default=True)],
        )
        assert oci.get_default_registry()["name"] == "second"

    def test_default_registry_falls_back_to_first_enabled(self, env):
        oci._save_registries(
            [_registry("off", enabled=False), _registry("on")],
        )
        assert oci.get_default_registry()["name"] == "on"

    def test_default_registry_falls_back_to_first_entry_when_all_disabled(self, env):
        oci._save_registries(
            [_registry("a", enabled=False), _registry("b", enabled=False)],
        )
        assert oci.get_default_registry()["name"] == "a"

    def test_default_registry_is_none_without_any_registries(self, env):
        assert oci.get_default_registry() is None


class TestRegistryConnection:
    def test_unknown_registry_is_not_connected(self, env):
        assert oci.test_registry_connection("nope") is False

    def test_registry_without_url_is_not_connected(self, env, fake_client):
        oci._save_registries([_registry("blank", url="")])
        assert oci.test_registry_connection("blank") is False
        assert fake_client.auth_seen == []  # never dialled out

    def test_reachable_registry_is_connected_even_with_no_tags(self, env, fake_client):
        oci._save_registries([_registry("reg", auth={"type": "token", "token": "t"})])
        fake_client.tags = []
        assert oci.test_registry_connection("reg") is True
        assert fake_client.auth_seen == [{"type": "token", "token": "t"}]

    def test_unreachable_registry_is_not_connected(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags_error = RuntimeError("401 Unauthorized")
        assert oci.test_registry_connection("reg") is False

    def test_list_registries_annotates_connectivity(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0"]
        regs = oci.list_registries()
        assert [r["connected"] for r in regs] == [True]


# ── Auth ─────────────────────────────────────────────────────────────────────


class TestAuthHeaders:
    def test_unknown_auth_type_produces_no_header(self):
        assert oci._auth_headers({"type": "mtls", "cert": "x"}) == {}

    def test_token_auth_without_a_token_produces_no_header(self):
        assert oci._auth_headers({"type": "token", "token": ""}) == {}

    def test_password_env_overrides_the_inline_password(self, monkeypatch):
        monkeypatch.setenv("REG_PW", "from-env")
        header = oci._auth_headers(
            {
                "type": "username_password",
                "username": "u",
                "password": "inline",
                "password_env": "REG_PW",
            }
        )
        assert header["Authorization"] == "Basic " + base64.b64encode(
            b"u:from-env"
        ).decode("ascii")

    def test_password_env_falls_back_to_inline_when_unset(self, monkeypatch):
        monkeypatch.delenv("REG_PW_MISSING", raising=False)
        header = oci._auth_headers(
            {
                "type": "username_password",
                "username": "u",
                "password": "inline",
                "password_env": "REG_PW_MISSING",
            }
        )
        assert header["Authorization"] == "Basic " + base64.b64encode(
            b"u:inline"
        ).decode("ascii")


# ── Pulling an index into a layout directory ─────────────────────────────────


class TestPullToLayout:
    def test_manifest_entries_without_a_digest_are_skipped(self, tmp_path, fake_client):
        fake_client.index_by_tag = {"1.0.0": {"manifests": [{}, {"digest": ""}]}}
        oci._oras_pull_to_layout("ghcr.io/x/y", "1.0.0", tmp_path / "layout")
        # Only the index itself was fetched; no per-recipe manifest requests.
        assert fake_client.manifest_refs == ["ghcr.io/x/y:1.0.0"]

    def test_layer_without_annotations_is_named_after_its_digest(
        self, tmp_path, fake_client
    ):
        fake_client.index_by_tag = {"1.0.0": {"manifests": [{"digest": "sha256:m1"}]}}
        fake_client.manifest_by_digest = {
            "sha256:m1": {"layers": [{"digest": "sha256:deadbeefcafe00", "size": 5}]}
        }
        fake_client.blobs = {"sha256:deadbeefcafe00": "name: anon"}

        layout = tmp_path / "layout"
        oci._oras_pull_to_layout("ghcr.io/x/y", "1.0.0", layout)

        assert [p.name for p in layout.glob("*.yaml")] == ["recipe-sha256:deadb.yaml"]

    @pytest.mark.parametrize(
        "title",
        [
            "../../../../escape.yaml",
            "/tmp/escape.yaml",
            "..",
            "subdir/escape.yaml",
        ],
    )
    def test_hostile_layer_titles_cannot_escape_the_layout_directory(
        self, tmp_path, fake_client, title
    ):
        """A registry-supplied title must never steer the write outside layout_dir."""
        fake_client.index_by_tag = {"1.0.0": {"manifests": [{"digest": "sha256:m1"}]}}
        fake_client.manifest_by_digest = {
            "sha256:m1": {
                "layers": [
                    {
                        "digest": "sha256:abc123456789",
                        "size": 5,
                        "annotations": {"org.opencontainers.image.title": title},
                    }
                ]
            }
        }
        fake_client.blobs = {"sha256:abc123456789": "name: evil"}

        layout = tmp_path / "root" / "layout"
        oci._oras_pull_to_layout("ghcr.io/x/y", "1.0.0", layout)

        written = [p for p in layout.rglob("*") if p.is_file()]
        assert len(written) == 1
        assert written[0].parent == layout
        # Nothing landed anywhere else under the sandbox.
        outside = [
            p
            for p in tmp_path.rglob("*")
            if p.is_file() and layout not in p.parents and p.parent != layout
        ]
        assert outside == []

    def test_hostile_recipe_name_annotation_is_also_sanitised(
        self, tmp_path, fake_client
    ):
        fake_client.index_by_tag = {"1.0.0": {"manifests": [{"digest": "sha256:m1"}]}}
        fake_client.manifest_by_digest = {
            "sha256:m1": {
                "layers": [
                    {
                        "digest": "sha256:abc123456789",
                        "size": 5,
                        "annotations": {"name": "../../pwned"},
                    }
                ]
            }
        }
        fake_client.blobs = {"sha256:abc123456789": "name: evil"}

        layout = tmp_path / "root" / "layout"
        oci._oras_pull_to_layout("ghcr.io/x/y", "1.0.0", layout)

        assert [p.name for p in layout.iterdir()] == ["pwned.yaml"]
        assert not (tmp_path / "pwned.yaml").exists()

    def test_safe_layer_filename_keeps_ordinary_names(self):
        assert oci._safe_layer_filename("Recipe.yaml", "sha256:abc") == "Recipe.yaml"
        assert oci._safe_layer_filename("", "sha256:abcdef012345") == (
            "recipe-sha256:abcde.yaml"
        )
        assert oci._safe_layer_filename("..", "") == "recipe-unknown.yaml"

    def test_pull_to_layout_creates_the_directory_and_delegates(
        self, tmp_path, fake_client
    ):
        fake_client.index_by_tag = {"2.0.0": {"manifests": []}}
        layout = tmp_path / "nested" / "layout"
        oci._pull_oci_to_layout("ghcr.io/x/y", "2.0.0", layout)
        assert layout.is_dir()
        assert fake_client.manifest_refs == ["ghcr.io/x/y:2.0.0"]

    def test_fetch_index_requests_the_tagged_reference(self, fake_client):
        fake_client.index_by_tag = {"1.2.3": _index()}
        got = oci._fetch_oci_index("ghcr.io/x/y", "1.2.3", auth={"type": "token"})
        assert got["annotations"]["name"] == "pack"
        assert fake_client.manifest_refs == ["ghcr.io/x/y:1.2.3"]
        assert fake_client.auth_seen == [{"type": "token"}]


# ── list_collections ─────────────────────────────────────────────────────────


class TestListCollections:
    def test_disabled_registries_and_urlless_registries_are_skipped(
        self, env, fake_client
    ):
        oci._save_registries(
            [_registry("off", enabled=False), _registry("blank", url="")]
        )
        assert oci.list_collections() == []
        assert fake_client.auth_seen == []

    def test_explicit_empty_version_returns_nothing(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0"]
        fake_client.index_by_tag = {"1.0.0": _index()}
        assert oci.list_collections(version="") == []

    def test_version_filter_selects_a_single_tag(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0", "2.0.0"]
        fake_client.index_by_tag = {
            "1.0.0": _index(tag_version="1.0.0"),
            "2.0.0": _index(tag_version="2.0.0"),
        }
        cols = oci.list_collections(version="1.0.0")
        assert [c.version for c in cols] == ["1.0.0"]
        assert cols[0].display_version == "1.0.0"

    def test_a_broken_index_does_not_hide_the_other_tags(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["broken", "1.0.0"]
        fake_client.index_by_tag = {"1.0.0": _index(manifests=[{"digest": "sha256:a"}])}
        cols = oci.list_collections()
        assert [(c.name, c.version, c.recipe_count) for c in cols] == [
            ("pack", "1.0.0", 1)
        ]

    def test_a_registry_that_cannot_list_tags_is_logged_and_skipped(
        self, env, monkeypatch, caplog
    ):
        oci._save_registries([_registry("bad"), _registry("good")])
        good = FakeRegistry(tags=["1.0.0"], index_by_tag={"1.0.0": _index()})

        def factory(auth=None):
            # First registry fails, second succeeds.
            if not getattr(factory, "called", False):
                factory.called = True
                return FakeClient(FakeRegistry(tags_error=RuntimeError("DNS")), auth)
            return FakeClient(good, auth)

        monkeypatch.setattr(oci, "_oras_client", factory)
        with caplog.at_level("WARNING"):
            cols = oci.list_collections()
        assert [c.registry for c in cols] == ["good"]
        assert "Failed to list tags for registry bad" in caplog.text

    def test_only_the_highest_version_of_a_collection_survives_dedup(
        self, env, fake_client
    ):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.9.0", "10.0.0", "latest"]
        fake_client.index_by_tag = {
            t: _index(tag_version=t) for t in ("1.9.0", "10.0.0", "latest")
        }
        cols = oci.list_collections()
        assert [c.version for c in cols] == ["10.0.0"]

    def test_non_numeric_versions_do_not_break_dedup(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["sha256:abc", "1.0.0"]
        fake_client.index_by_tag = {
            "sha256:abc": _index(tag_version="sha256:abc"),
            "1.0.0": _index(tag_version="1.0.0"),
        }
        cols = oci.list_collections()
        assert [c.version for c in cols] == ["1.0.0"]

    def test_a_cache_hit_avoids_a_second_index_fetch(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0"]
        fake_client.index_by_tag = {"1.0.0": _index(manifests=[{"digest": "sha256:a"}])}

        first = oci.list_collections()
        fetches = len(fake_client.manifest_refs)
        second = oci.list_collections()

        assert len(fake_client.manifest_refs) == fetches  # served from cache
        assert [c.name for c in second] == [c.name for c in first]
        assert second[0].recipe_count == 1
        assert second[0].vendor == "acme"


# ── _extract_recipe_from_layer ───────────────────────────────────────────────


class TestExtractRecipeFromLayer:
    def _client(self, fake_registry):
        return lambda: FakeClient(fake_registry)

    def test_metadata_is_read_from_the_layer_yaml(self):
        registry = FakeRegistry(
            manifest_by_digest={
                "sha256:m1": {"layers": [{"digest": "sha256:blob1"}]},
            },
            blobs={
                "sha256:blob1": yaml.safe_dump(
                    {
                        "name": "Llama",
                        "description": "a recipe",
                        "model": "meta/llama",
                        "container": "vllm:latest",
                        "cluster_only": 1,
                    }
                )
            },
        )
        with patch("oras.client.OrasClient", self._client(registry)):
            info = oci._extract_recipe_from_layer(
                "ghcr.io/x/y", {"digest": "sha256:m1"}, "1.0.0"
            )
        assert info == {
            "name": "Llama",
            "description": "a recipe",
            "model": "meta/llama",
            "container": "vllm:latest",
            "recipe_version": "1.0.0",
            "solo_only": False,
            "cluster_only": True,
        }

    def test_auth_headers_are_applied_to_the_client_session(self):
        registry = FakeRegistry(
            manifest_by_digest={"sha256:m1": {"layers": [{"digest": "sha256:b"}]}},
            blobs={"sha256:b": "name: x"},
        )
        created = []

        def factory():
            client = FakeClient(registry)
            created.append(client)
            return client

        with patch("oras.client.OrasClient", factory):
            oci._extract_recipe_from_layer(
                "ghcr.io/x/y",
                {"digest": "sha256:m1"},
                "1.0.0",
                auth={"type": "token", "token": "sec"},
            )
        assert created[0].session.headers["Authorization"] == "Bearer sec"

    def test_name_defaults_to_the_digest_tail(self):
        registry = FakeRegistry(
            manifest_by_digest={"sha256:m1": {"layers": [{"digest": "sha256:b"}]}},
            blobs={"sha256:b": "model: m\n"},
        )
        with patch("oras.client.OrasClient", self._client(registry)):
            info = oci._extract_recipe_from_layer(
                "ghcr.io/x/y", {"digest": "sha256:m1"}, "1.0.0"
            )
        assert info["name"] == "m1"

    def test_missing_digest_is_rejected(self):
        registry = FakeRegistry()
        with patch("oras.client.OrasClient", self._client(registry)):
            with pytest.raises(ValueError, match="No digest"):
                oci._extract_recipe_from_layer("ghcr.io/x/y", {}, "1.0.0")

    def test_manifest_without_layers_is_rejected(self):
        registry = FakeRegistry(manifest_by_digest={"sha256:m1": {"layers": []}})
        with patch("oras.client.OrasClient", self._client(registry)):
            with pytest.raises(ValueError, match="No layers"):
                oci._extract_recipe_from_layer(
                    "ghcr.io/x/y", {"digest": "sha256:m1"}, "1.0.0"
                )

    def test_layer_without_a_digest_is_rejected(self):
        registry = FakeRegistry(manifest_by_digest={"sha256:m1": {"layers": [{}]}})
        with patch("oras.client.OrasClient", self._client(registry)):
            with pytest.raises(ValueError, match="No layer digest"):
                oci._extract_recipe_from_layer(
                    "ghcr.io/x/y", {"digest": "sha256:m1"}, "1.0.0"
                )

    def test_blob_download_failure_propagates(self):
        registry = FakeRegistry(
            manifest_by_digest={"sha256:m1": {"layers": [{"digest": "sha256:b"}]}}
        )
        registry.blob_error = RuntimeError("403 Forbidden")
        with patch("oras.client.OrasClient", self._client(registry)):
            with pytest.raises(RuntimeError, match="403 Forbidden"):
                oci._extract_recipe_from_layer(
                    "ghcr.io/x/y", {"digest": "sha256:m1"}, "1.0.0"
                )

    def test_malformed_yaml_propagates(self):
        registry = FakeRegistry(
            manifest_by_digest={"sha256:m1": {"layers": [{"digest": "sha256:b"}]}},
            blobs={"sha256:b": "name: [unterminated"},
        )
        with patch("oras.client.OrasClient", self._client(registry)):
            with pytest.raises(yaml.YAMLError):
                oci._extract_recipe_from_layer(
                    "ghcr.io/x/y", {"digest": "sha256:m1"}, "1.0.0"
                )


# ── list_collection_recipes ──────────────────────────────────────────────────


class TestListCollectionRecipes:
    def test_unknown_registry_is_an_error(self, env):
        with pytest.raises(ValueError, match="not found"):
            oci.list_collection_recipes("pack", registry_name="nope")

    def test_annotated_entries_are_used_directly(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0"]
        fake_client.index_by_tag = {
            "1.0.0": _index(
                manifests=[
                    {
                        "digest": "sha256:m1",
                        "annotations": {
                            "name": "llama",
                            "model": "meta/llama",
                            "container": "vllm",
                            "org.opencontainers.image.description": "big",
                            "recipe_version": "3.1",
                            "solo_only": True,
                        },
                    }
                ]
            )
        }
        recipes = oci.list_collection_recipes("pack")
        assert len(recipes) == 1
        r = recipes[0]
        assert (r.name, r.model, r.container, r.description) == (
            "llama",
            "meta/llama",
            "vllm",
            "big",
        )
        assert r.recipe_version == "3.1"
        assert r.solo_only is True and r.cluster_only is False

    def test_other_collections_in_the_registry_are_ignored(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0"]
        fake_client.index_by_tag = {
            "1.0.0": _index(
                name="other-pack",
                manifests=[{"digest": "sha256:m1", "annotations": {"name": "x"}}],
            )
        }
        assert oci.list_collection_recipes("pack") == []

    def test_unannotated_entries_fall_back_to_the_layer_yaml(self, env, fake_client):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0"]
        fake_client.index_by_tag = {
            "1.0.0": _index(manifests=[{"digest": "sha256:m1", "annotations": {}}])
        }
        with patch.object(
            oci,
            "_extract_recipe_from_layer",
            return_value={
                "name": "from-yaml",
                "description": "d",
                "model": "m",
                "container": "c",
                "recipe_version": "1.0.0",
                "solo_only": False,
                "cluster_only": True,
            },
        ) as extractor:
            recipes = oci.list_collection_recipes("pack")
        assert [r.name for r in recipes] == ["from-yaml"]
        assert recipes[0].cluster_only is True
        assert extractor.call_args.kwargs["auth"] == {}

    def test_layer_extraction_failure_degrades_to_a_digest_named_stub(
        self, env, fake_client
    ):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0"]
        fake_client.index_by_tag = {
            "1.0.0": _index(manifests=[{"digest": "sha256:m1", "annotations": {}}])
        }
        with patch.object(
            oci, "_extract_recipe_from_layer", side_effect=RuntimeError("blob gone")
        ):
            recipes = oci.list_collection_recipes("pack")
        assert len(recipes) == 1
        assert recipes[0].name == "sha256:m1"
        assert recipes[0].model == "" and recipes[0].recipe_version == "1.0.0"

    def test_broken_index_and_unreachable_registry_are_tolerated(
        self, env, fake_client, caplog
    ):
        oci._save_registries([_registry("reg")])
        fake_client.tags = ["broken"]
        with caplog.at_level("WARNING"):
            assert oci.list_collection_recipes("pack") == []

        fake_client.tags_error = RuntimeError("timeout")
        with caplog.at_level("WARNING"):
            assert oci.list_collection_recipes("pack") == []
        assert "Failed to list tags for registry reg" in caplog.text

    def test_disabled_registries_and_empty_version_short_circuit(
        self, env, fake_client
    ):
        oci._save_registries([_registry("off", enabled=False)])
        assert oci.list_collection_recipes("pack") == []

        oci._save_registries([_registry("reg")])
        fake_client.tags = ["1.0.0"]
        fake_client.index_by_tag = {"1.0.0": _index()}
        assert oci.list_collection_recipes("pack", version="") == []

    def test_a_registry_without_a_url_is_skipped(self, env, fake_client):
        oci._save_registries([_registry("blank", url="")])
        assert oci.list_collection_recipes("pack", registry_name="blank") == []
        assert fake_client.auth_seen == []

    def test_the_named_registry_and_version_narrow_the_search(self, env, fake_client):
        oci._save_registries([_registry("other"), _registry("reg")])
        fake_client.tags = ["1.0.0", "2.0.0"]
        entry = {"digest": "sha256:m1", "annotations": {"name": "llama"}}
        fake_client.index_by_tag = {
            "1.0.0": _index(manifests=[entry]),
            "2.0.0": _index(manifests=[entry, dict(entry, digest="sha256:m2")]),
        }

        recipes = oci.list_collection_recipes(
            "pack", registry_name="reg", version="2.0.0"
        )

        assert [r.recipe_version for r in recipes] == ["2.0.0", "2.0.0"]


# ── install_collection ───────────────────────────────────────────────────────


def _stock_registry(fake_client, tag="1.0.0", recipes=(("alpha", "name: alpha"),)):
    """Wire the fake registry up as a collection with one manifest per recipe."""
    manifests = []
    fake_client.manifest_by_digest = {}
    fake_client.blobs = {}
    for name, body in recipes:
        manifests.append({"digest": f"sha256:{name}-manifest"})
        fake_client.manifest_by_digest[f"sha256:{name}-manifest"] = {
            "layers": [
                {
                    "digest": f"sha256:{name}-blob",
                    "size": len(body),
                    "annotations": {"org.opencontainers.image.title": f"{name}.yaml"},
                }
            ]
        }
        fake_client.blobs[f"sha256:{name}-blob"] = body
    fake_client.tags = [tag]
    fake_client.index_by_tag = {tag: _index(tag_version=tag, manifests=manifests)}


class TestInstallCollection:
    def test_unknown_registry_is_an_error(self, env):
        with pytest.raises(ValueError, match="Registry 'nope' not found"):
            oci.install_collection("pack", "1.0.0", registry_name="nope")

    def test_unknown_collection_version_is_an_error(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        with pytest.raises(ValueError, match="'pack:9.9.9' not found"):
            oci.install_collection("pack", "9.9.9")

    def test_a_pull_failure_becomes_a_runtime_error(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        with patch.object(oci, "_pull_oci_to_layout", side_effect=OSError("disk full")):
            with pytest.raises(RuntimeError, match="Failed to pull OCI image"):
                oci.install_collection("pack", "1.0.0")

    def test_a_collection_with_no_recipe_files_is_an_error(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        with patch.object(oci, "_pull_oci_to_layout"):  # pulls nothing
            with pytest.raises(RuntimeError, match="No recipe files found"):
                oci.install_collection("pack", "1.0.0")

    def test_install_writes_recipes_and_metadata_sidecars(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(
            fake_client, recipes=(("alpha", "name: alpha"), ("beta", "name: beta"))
        )

        installed = oci.install_collection("pack", "1.0.0")

        assert sorted(installed) == ["alpha.yaml", "beta.yaml"]
        assert (env.recipes_dir / "alpha.yaml").read_text() == "name: alpha"
        meta = yaml.safe_load((env.recipes_dir / "alpha.yaml.meta").read_text())
        assert meta["source"] == "reg"
        assert meta["collection"] == "pack"
        assert meta["version"] == "1.0.0"
        assert meta["digest"] == (
            "sha256:" + hashlib.sha256(b"name: alpha").hexdigest()
        )
        # A freshly installed recipe is not "locally modified".
        assert oci.get_oci_meta("alpha.yaml").local_changes is False

    def test_install_overwrites_a_locally_modified_recipe_and_warns(
        self, env, fake_client, caplog
    ):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        (env.recipes_dir / "alpha.yaml").write_text("name: my-edit")

        with caplog.at_level("WARNING"):
            oci.install_collection("pack", "1.0.0")

        assert (env.recipes_dir / "alpha.yaml").read_text() == "name: alpha"
        assert "Local modifications detected" in caplog.text

    @needs_unprivileged
    def test_an_unreadable_existing_recipe_is_still_replaced(self, env, fake_client):
        """The modification check is best-effort: an unreadable file is replaced."""
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        dest = env.recipes_dir / "alpha.yaml"
        dest.write_text("name: locked")
        dest.chmod(0o222)  # writable, not readable
        try:
            installed = oci.install_collection("pack", "1.0.0")
        finally:
            dest.chmod(0o644)
        assert installed == ["alpha.yaml"]
        assert dest.read_text() == "name: alpha"

    def test_dry_run_touches_nothing(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        assert oci.install_collection("pack", "1.0.0", dry_run=True) == []
        assert list(env.recipes_dir.iterdir()) == []


# ── install / update / uninstall of a single recipe ──────────────────────────


class TestInstallOciRecipe:
    def test_no_registries_configured_is_an_error(self, env):
        with pytest.raises(ValueError, match="No registries configured"):
            oci.install_oci_recipe("pack", "alpha", "1.0.0")

    def test_unknown_registry_is_an_error(self, env):
        with pytest.raises(ValueError, match="Registry 'nope' not found"):
            oci.install_oci_recipe("pack", "alpha", "1.0.0", registry_name="nope")

    def test_pull_failure_becomes_a_runtime_error(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        with patch.object(oci, "_pull_oci_to_layout", side_effect=OSError("no route")):
            with pytest.raises(RuntimeError, match="Failed to pull OCI image"):
                oci.install_oci_recipe("pack", "alpha", "1.0.0")

    def test_recipe_missing_from_the_collection_is_an_error(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        with pytest.raises(ValueError, match="Recipe 'gamma' not found"):
            oci.install_oci_recipe("pack", "gamma", "1.0.0")

    def test_first_install_writes_the_file_and_its_meta(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)

        result = oci.install_oci_recipe("pack", "alpha", "1.0.0")

        assert result == {"success": True, "recipe": "alpha", "action": "installed"}
        assert (env.recipes_dir / "alpha.yaml").read_text() == "name: alpha"
        assert oci.get_oci_meta("alpha.yaml").collection == "pack"

    def test_reinstalling_identical_content_reports_up_to_date(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        oci.install_oci_recipe("pack", "alpha", "1.0.0")
        meta_before = (env.recipes_dir / "alpha.yaml.meta").read_text()

        result = oci.install_oci_recipe("pack", "alpha", "1.0.0")

        assert result["action"] == "up_to_date"
        assert (env.recipes_dir / "alpha.yaml.meta").read_text() == meta_before

    def test_changed_upstream_content_reports_updated(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        (env.recipes_dir / "alpha.yaml").write_text("name: stale")

        result = oci.install_oci_recipe("pack", "alpha", "1.0.0")

        assert result["action"] == "updated"
        assert (env.recipes_dir / "alpha.yaml").read_text() == "name: alpha"

    def test_overwrite_skips_the_comparison_entirely(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        (env.recipes_dir / "alpha.yaml").write_text("name: stale")

        result = oci.install_oci_recipe("pack", "alpha", "1.0.0", overwrite=True)

        assert result["action"] == "installed"
        assert (env.recipes_dir / "alpha.yaml").read_text() == "name: alpha"

    @needs_unprivileged
    def test_an_unreadable_existing_recipe_counts_as_an_update(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client)
        dest = env.recipes_dir / "alpha.yaml"
        dest.write_text("name: locked")
        dest.chmod(0o222)  # writable, not readable
        try:
            result = oci.install_oci_recipe("pack", "alpha", "1.0.0")
        finally:
            dest.chmod(0o644)

        assert result["action"] == "updated"
        assert dest.read_text() == "name: alpha"

    def test_a_yml_recipe_is_matched_by_its_stem(self, env, fake_client):
        oci._save_registries([_registry("reg", default=True)])
        _stock_registry(fake_client, recipes=(("gamma", "name: gamma"),))
        # Re-title the layer so it lands as .yml.
        fake_client.manifest_by_digest["sha256:gamma-manifest"]["layers"][0][
            "annotations"
        ] = {"org.opencontainers.image.title": "gamma.yml"}

        result = oci.install_oci_recipe("pack", "gamma", "1.0.0")

        assert result["action"] == "installed"
        assert (env.recipes_dir / "gamma.yml").read_text() == "name: gamma"


class TestUpdateOciRecipe:
    def test_updating_a_recipe_that_is_not_oci_installed_is_an_error(self, env):
        with pytest.raises(ValueError, match="not an OCI-installed recipe"):
            oci.update_oci_recipe("alpha.yaml", "pack")

    def test_version_and_registry_default_to_the_recorded_metadata(self, env):
        (env.recipes_dir / "alpha.yaml").write_text("name: alpha")
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", "sha256:x")

        with patch.object(
            oci, "install_oci_recipe", return_value={"action": "updated"}
        ) as install:
            oci.update_oci_recipe("alpha.yaml", "pack")

        assert install.call_args.kwargs == {
            "collection_name": "pack",
            "recipe_name": "alpha.yaml",
            "version": "1.0.0",
            "registry_name": "reg",
            "overwrite": True,
        }

    def test_explicit_version_and_registry_win(self, env):
        (env.recipes_dir / "alpha.yaml").write_text("name: alpha")
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", "sha256:x")

        with patch.object(
            oci, "install_oci_recipe", return_value={"action": "updated"}
        ) as install:
            oci.update_oci_recipe(
                "alpha.yaml", "pack", version="2.0.0", registry_name="other"
            )

        assert install.call_args.kwargs["version"] == "2.0.0"
        assert install.call_args.kwargs["registry_name"] == "other"


class TestUninstallOciRecipe:
    def test_unknown_recipe_reports_not_found(self, env):
        assert oci.uninstall_oci_recipe("ghost.yaml") == {
            "success": False,
            "recipe": "ghost.yaml",
            "action": "not_found",
        }

    def test_uninstall_removes_the_recipe_and_its_sidecar(self, env):
        (env.recipes_dir / "alpha.yaml").write_text("name: alpha")
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", "sha256:x")

        result = oci.uninstall_oci_recipe("alpha.yaml")

        assert result["success"] is True and result["action"] == "uninstalled"
        assert len(result["removed"]) == 2
        assert list(env.recipes_dir.iterdir()) == []

    def test_uninstalling_a_yml_recipe_also_removes_its_sidecar(self, env):
        """The sidecar for ``x.yml`` is written as ``x.yaml.meta`` — remove both."""
        (env.recipes_dir / "gamma.yml").write_text("name: gamma")
        oci._write_recipe_meta("gamma.yml", "reg", "pack", "1.0.0", "sha256:x")
        assert (env.recipes_dir / "gamma.yaml.meta").exists()

        result = oci.uninstall_oci_recipe("gamma.yml")

        assert result["success"] is True
        assert list(env.recipes_dir.iterdir()) == []


# ── Metadata sidecars ────────────────────────────────────────────────────────


class TestRecipeMetadata:
    def test_meta_path_for_an_extensionless_name(self, env):
        assert oci._meta_path("alpha").name == "alpha.meta"
        assert oci._meta_path("alpha.yml").name == "alpha.yaml.meta"

    def test_rewriting_metadata_preserves_the_original_install_time(self, env):
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", "sha256:a")
        first = yaml.safe_load((env.recipes_dir / "alpha.yaml.meta").read_text())

        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "2.0.0", "sha256:b")
        second = yaml.safe_load((env.recipes_dir / "alpha.yaml.meta").read_text())

        assert second["installed_at"] == first["installed_at"]
        assert second["version"] == "2.0.0"
        assert second["digest"] == "sha256:b"

    def test_a_corrupt_sidecar_is_replaced_rather_than_crashing_the_write(self, env):
        (env.recipes_dir / "alpha.yaml.meta").write_text("[not: valid: yaml")
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", "sha256:a")
        meta = yaml.safe_load((env.recipes_dir / "alpha.yaml.meta").read_text())
        assert meta["version"] == "1.0.0"
        assert meta["installed_at"] == meta["updated_at"]

    def test_a_corrupt_sidecar_reads_back_as_no_metadata(self, env):
        (env.recipes_dir / "alpha.yaml.meta").write_text("{unclosed")
        assert oci._read_recipe_meta("alpha.yaml") is None

    def test_an_untouched_recipe_is_not_flagged_as_locally_modified(self, env):
        """Regression: the digest is stored as ``sha256:<hex>`` and must be
        compared against the hex hash, not against the prefixed string."""
        body = "name: alpha\n"
        (env.recipes_dir / "alpha.yaml").write_text(body)
        digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", digest)

        meta = oci._read_recipe_meta("alpha.yaml")
        assert meta.local_changes is False
        assert meta.name == "alpha.yaml"
        assert meta.source == "reg"

    def test_an_edited_recipe_is_flagged_as_locally_modified(self, env):
        body = "name: alpha\n"
        digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", digest)
        (env.recipes_dir / "alpha.yaml").write_text("name: alpha-edited\n")

        assert oci._read_recipe_meta("alpha.yaml").local_changes is True

    def test_a_sidecar_without_a_digest_flags_the_recipe_as_modified(self, env):
        (env.recipes_dir / "alpha.yaml").write_text("name: alpha\n")
        (env.recipes_dir / "alpha.yaml.meta").write_text(
            yaml.safe_dump({"source": "reg", "collection": "pack", "version": "1.0.0"})
        )
        assert oci._read_recipe_meta("alpha.yaml").local_changes is True

    @needs_unprivileged
    def test_an_unreadable_recipe_is_not_guessed_to_be_modified(self, env):
        recipe = env.recipes_dir / "alpha.yaml"
        recipe.write_text("name: alpha\n")
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", "sha256:a")
        recipe.chmod(0o000)
        try:
            meta = oci._read_recipe_meta("alpha.yaml")
        finally:
            recipe.chmod(0o644)
        assert meta is not None and meta.local_changes is False

    def test_listing_oci_recipes_without_a_recipes_dir(self, env, monkeypatch):
        monkeypatch.setattr(oci, "RECIPES_DIR", env.tmp / "absent")
        assert oci.list_oci_recipes() == []

    def test_listing_oci_recipes_covers_only_recipes_with_sidecars(self, env):
        (env.recipes_dir / "alpha.yaml").write_text("name: alpha")
        (env.recipes_dir / "plain.yaml").write_text("name: plain")
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", "sha256:a")

        metas = oci.list_oci_recipes()

        assert [m.name for m in metas] == ["alpha.yaml"]
        assert metas[0].collection == "pack"

    def test_get_oci_meta_is_a_read_of_the_sidecar(self, env):
        assert oci.get_oci_meta("absent.yaml") is None
        oci._write_recipe_meta("alpha.yaml", "reg", "pack", "1.0.0", "sha256:a")
        assert oci.get_oci_meta("alpha.yaml").version == "1.0.0"


# ── check_updates ────────────────────────────────────────────────────────────


def _collection(name="pack", version="2.0.0", registry="reg"):
    return oci.CollectionInfo(
        name=name,
        version=version,
        description="",
        vendor="",
        license="",
        recipe_count=1,
        digest=f"sha256:{version}",
        registry=registry,
    )


class TestCheckUpdates:
    def _install_meta(self, env, version="1.0.0", registry="reg", collection="pack"):
        body = "name: alpha\n"
        (env.recipes_dir / "alpha.yaml").write_text(body)
        digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        oci._write_recipe_meta("alpha.yaml", registry, collection, version, digest)

    def test_a_newer_registry_version_is_reported(self, env):
        self._install_meta(env)
        with patch.object(oci, "list_collections", return_value=[_collection()]):
            updates = oci.check_updates()

        assert len(updates) == 1
        upd = updates[0]
        assert (upd.collection, upd.current_version, upd.latest_version) == (
            "pack",
            "1.0.0",
            "2.0.0",
        )
        assert upd.latest_digest == "sha256:2.0.0"
        assert upd.local_changes is False

    def test_local_edits_are_surfaced_on_the_update(self, env):
        self._install_meta(env)
        (env.recipes_dir / "alpha.yaml").write_text("name: hand-edited\n")
        with patch.object(oci, "list_collections", return_value=[_collection()]):
            assert oci.check_updates()[0].local_changes is True

    def test_a_recipe_already_at_the_latest_version_is_not_reported(self, env):
        self._install_meta(env, version="2.0.0")
        with patch.object(oci, "list_collections", return_value=[_collection()]):
            assert oci.check_updates() == []

    def test_a_collection_absent_from_the_registry_is_not_reported(self, env):
        self._install_meta(env)
        with patch.object(
            oci, "list_collections", return_value=[_collection(name="other")]
        ):
            assert oci.check_updates() == []

    def test_collection_and_registry_filters_are_applied(self, env):
        self._install_meta(env)
        with patch.object(oci, "list_collections", return_value=[_collection()]) as lc:
            assert oci.check_updates(collection="other-pack") == []
            assert oci.check_updates(registry="other-reg") == []
            assert lc.call_count == 0  # filtered before any network call

    def test_a_registry_error_is_logged_and_skipped(self, env, caplog):
        self._install_meta(env)
        with patch.object(oci, "list_collections", side_effect=RuntimeError("502")):
            with caplog.at_level("WARNING"):
                assert oci.check_updates() == []
        assert "Failed to check registry for pack" in caplog.text


# ── apply_updates / run_auto_update ──────────────────────────────────────────


class TestRunAutoUpdate:
    def _config(self, enabled=True, overwrite=False):
        return SimpleNamespace(
            oci_auto_update_enabled=enabled,
            oci_auto_update_overwrite_local=overwrite,
        )

    def _update(self, local_changes=False):
        return oci.UpdateInfo(
            collection="pack",
            current_version="1.0.0",
            latest_version="2.0.0",
            current_digest="sha256:old",
            latest_digest="sha256:new",
            local_changes=local_changes,
        )

    def test_disabled_auto_update_does_nothing(self, env, monkeypatch):
        monkeypatch.setattr(oci, "config", self._config(enabled=False))
        with patch.object(oci, "check_updates") as check:
            assert oci.run_auto_update() == {
                "skipped": True,
                "reason": "Auto-update disabled",
            }
        check.assert_not_called()

    def test_no_updates_available(self, env, monkeypatch):
        monkeypatch.setattr(oci, "config", self._config())
        with patch.object(oci, "check_updates", return_value=[]):
            result = oci.run_auto_update()
        assert result["success"] is True and result["updated"] == 0
        assert any("No updates available" in line for line in result["log"])

    def test_available_updates_are_installed(self, env, monkeypatch):
        monkeypatch.setattr(oci, "config", self._config())
        with patch.object(oci, "check_updates", return_value=[self._update()]):
            with patch.object(
                oci, "install_collection", return_value=["a.yaml", "b.yaml"]
            ) as install:
                result = oci.run_auto_update()

        assert result == {
            "success": True,
            "updated": 2,
            "log": result["log"],
        }
        assert install.call_args.kwargs == {
            "name": "pack",
            "version": "2.0.0",
            "registry_name": None,
        }
        assert any("Updated pack: 2 recipes installed" in ln for ln in result["log"])

    def test_locally_modified_collections_are_skipped_by_default(
        self, env, monkeypatch
    ):
        monkeypatch.setattr(oci, "config", self._config(overwrite=False))
        with patch.object(
            oci, "check_updates", return_value=[self._update(local_changes=True)]
        ):
            with patch.object(oci, "install_collection") as install:
                result = oci.run_auto_update()

        assert result["updated"] == 0
        install.assert_not_called()
        assert any("local changes detected" in line for line in result["log"])

    def test_overwrite_local_forces_the_update_through(self, env, monkeypatch):
        monkeypatch.setattr(oci, "config", self._config(overwrite=True))
        with patch.object(
            oci, "check_updates", return_value=[self._update(local_changes=True)]
        ):
            with patch.object(oci, "install_collection", return_value=["a.yaml"]):
                result = oci.run_auto_update()
        assert result["updated"] == 1

    def test_a_failed_install_is_recorded_in_the_log(self, env, monkeypatch):
        monkeypatch.setattr(oci, "config", self._config())
        with patch.object(oci, "check_updates", return_value=[self._update()]):
            with patch.object(
                oci, "install_collection", side_effect=RuntimeError("registry down")
            ):
                result = oci.run_auto_update()

        assert result["updated"] == 0
        assert any("Failed to update pack: registry down" in ln for ln in result["log"])

    def test_a_crash_during_the_check_is_reported(self, env, monkeypatch):
        monkeypatch.setattr(oci, "config", self._config())
        with patch.object(oci, "check_updates", side_effect=RuntimeError("boom")):
            result = oci.run_auto_update()
        assert result["success"] is False
        assert result["error"] == "boom"
        assert any("Auto-update failed: boom" in line for line in result["log"])

    def test_save_auto_update_log_is_a_noop(self):
        assert oci.save_auto_update_log() is None


class TestApplyUpdates:
    def test_each_update_is_installed_and_reported(self, env):
        with patch.object(
            oci, "install_collection", side_effect=[["a.yaml"], ValueError("nope")]
        ):
            results = oci.apply_updates(
                [
                    {
                        "collection": "pack",
                        "target_version": "2.0.0",
                        "registry": "reg",
                    },
                    {"collection": "other", "target_version": "1.0.0"},
                ]
            )

        assert results[0] == {
            "collection": "pack",
            "success": True,
            "installed": ["a.yaml"],
        }
        assert results[1]["success"] is False and results[1]["error"] == "nope"
