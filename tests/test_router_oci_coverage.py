"""The `/api/oci` router in *real* mode, plus the simulation branches
`tests/test_router_oci.py` does not reach.

`tests/test_router_oci.py` drives the happy paths with ``is_simulation()``
forced to ``True``. Here ``is_simulation()`` is forced to ``False`` so the
router's real branches run, with every ``spark_pulse.tools.oci_registry``
call replaced by a mock — no registry is contacted and nothing is written.
"""

from __future__ import annotations

from unittest.mock import call, patch

import pytest
from fastapi.testclient import TestClient

from spark_pulse.app import create_app
from spark_pulse.routers import oci as oci_router
from spark_pulse.tools.oci_registry import (
    CollectionInfo,
    CollectionRecipe,
    RecipeMeta,
    UpdateInfo,
)


@pytest.fixture(scope="module")
def app():
    return create_app()


@pytest.fixture
def client(app):
    """A client whose requests take the router's real (non-simulated) path."""
    with patch.object(oci_router, "is_simulation", return_value=False):
        yield TestClient(app)


@pytest.fixture
def sim_client(app):
    """A client whose requests take the router's simulation path."""
    with patch.object(oci_router, "is_simulation", return_value=True):
        yield TestClient(app)


def _meta(name="spark-vllm-7b.yaml", local_changes=False):
    return RecipeMeta(
        name=name,
        source="spark-official",
        collection="spark-recipes",
        version="1.0.0",
        digest="sha256:abc123",
        installed_at="2026-06-15T02:00:00Z",
        updated_at="2026-06-16T02:00:00Z",
        local_changes=local_changes,
    )


# ── Registries ───────────────────────────────────────────────────────────────


class TestRegistries:
    def test_list_returns_what_the_tool_layer_reports(self, client):
        regs = [{"name": "spark-official", "url": "ghcr.io/x", "connected": True}]
        with patch.object(oci_router, "list_registries", return_value=regs):
            response = client.get("/api/oci/registries")
        assert response.status_code == 200
        assert response.json() == regs

    def test_create_requires_name_and_url(self, client):
        with patch.object(oci_router, "add_registry") as add:
            response = client.post("/api/oci/registries", json={"name": "only-a-name"})
        assert response.status_code == 400
        assert response.json()["detail"] == "name and url are required"
        add.assert_not_called()

    def test_create_forwards_the_whole_body(self, client):
        body = {"name": "mine", "url": "registry.example.com/r", "auth": {"token": "t"}}
        with patch.object(
            oci_router, "add_registry", return_value={**body, "enabled": True}
        ) as add:
            response = client.post("/api/oci/registries", json=body)
        assert response.status_code == 200
        assert response.json()["enabled"] is True
        assert add.call_args == call(body)

    def test_create_reports_a_rejected_registry_as_400(self, client):
        with patch.object(
            oci_router, "add_registry", side_effect=ValueError("bad url")
        ):
            response = client.post(
                "/api/oci/registries", json={"name": "m", "url": "://"}
            )
        assert response.status_code == 400
        assert response.json()["detail"] == "bad url"

    def test_update_returns_the_updated_registry(self, client):
        updated = {"name": "mine", "url": "u", "enabled": False}
        with patch.object(oci_router, "update_registry", return_value=updated) as upd:
            response = client.put("/api/oci/registries/mine", json={"enabled": False})
        assert response.status_code == 200
        assert response.json() == updated
        assert upd.call_args == call("mine", {"enabled": False})

    def test_update_of_an_unknown_registry_is_404(self, client):
        with patch.object(oci_router, "update_registry", return_value=None):
            response = client.put("/api/oci/registries/ghost", json={"enabled": True})
        assert response.status_code == 404
        assert response.json()["detail"] == "Registry 'ghost' not found"

    def test_delete_confirms_removal(self, client):
        with patch.object(oci_router, "remove_registry", return_value=True) as remove:
            response = client.delete("/api/oci/registries/mine")
        assert response.status_code == 200
        assert response.json() == {"deleted": True}
        assert remove.call_args == call("mine")

    def test_delete_of_an_unknown_registry_is_404(self, client):
        with patch.object(oci_router, "remove_registry", return_value=False):
            response = client.delete("/api/oci/registries/ghost")
        assert response.status_code == 404
        assert response.json()["detail"] == "Registry 'ghost' not found"

    def test_test_connection_reports_a_failed_probe_without_erroring(self, client):
        with patch.object(
            oci_router, "test_registry_connection", return_value=False
        ) as probe:
            response = client.get("/api/oci/registries/mine/test-connection")
        assert response.status_code == 200
        assert response.json() == {"ok": False, "registry": "mine"}
        assert probe.call_args == call("mine")


class TestRegistryVersions:
    def test_tags_come_back_newest_first(self, client):
        reg = {"name": "mine", "url": "ghcr.io/x/r", "auth": {"token": "t"}}
        with patch.object(oci_router, "list_registries", return_value=[reg]):
            with patch.object(
                oci_router, "_oras_list_tags", return_value=["1.0.0", "1.2.0", "1.1.0"]
            ) as tags:
                response = client.get("/api/oci/registries/mine/versions")
        assert response.status_code == 200
        assert response.json() == {"versions": ["1.2.0", "1.1.0", "1.0.0"]}
        assert tags.call_args == call("ghcr.io/x/r", auth={"token": "t"})

    def test_unknown_registry_is_404_not_500(self, client):
        with patch.object(oci_router, "list_registries", return_value=[]):
            response = client.get("/api/oci/registries/ghost/versions")
        assert response.status_code == 404
        assert response.json()["detail"] == "Registry 'ghost' not found"

    def test_a_registry_without_a_url_has_no_versions(self, client):
        with patch.object(
            oci_router, "list_registries", return_value=[{"name": "mine"}]
        ):
            with patch.object(oci_router, "_oras_list_tags") as tags:
                response = client.get("/api/oci/registries/mine/versions")
        assert response.status_code == 200
        assert response.json() == {"versions": []}
        tags.assert_not_called()

    def test_a_registry_auth_failure_surfaces_as_500(self, client):
        with patch.object(
            oci_router,
            "list_registries",
            return_value=[{"name": "mine", "url": "ghcr.io/x"}],
        ):
            with patch.object(
                oci_router,
                "_oras_list_tags",
                side_effect=RuntimeError("401 unauthorized"),
            ):
                response = client.get("/api/oci/registries/mine/versions")
        assert response.status_code == 500
        assert response.json()["detail"] == "401 unauthorized"


# ── Collections ──────────────────────────────────────────────────────────────


class TestCollections:
    def test_collections_are_serialised_field_by_field(self, client):
        collection = CollectionInfo(
            name="spark-recipes",
            version="sha-abc",
            description="Spark Pulse recipes",
            vendor="KEL",
            license="MIT",
            recipe_count=5,
            digest="sha256:abc",
            registry="ghcr.io/x/r",
            display_version="1.0.0",
        )
        with patch.object(
            oci_router, "list_collections", return_value=[collection]
        ) as lister:
            response = client.get("/api/oci/collections?registry=ghcr&version=1.0.0")
        assert response.status_code == 200
        assert response.json() == [
            {
                "name": "spark-recipes",
                "version": "sha-abc",
                "display_version": "1.0.0",
                "description": "Spark Pulse recipes",
                "vendor": "KEL",
                "license": "MIT",
                "recipe_count": 5,
                "digest": "sha256:abc",
                "registry": "ghcr.io/x/r",
            }
        ]
        assert lister.call_args == call(registry_name="ghcr", version="1.0.0")

    def test_display_version_falls_back_to_the_tag(self, client):
        collection = CollectionInfo(
            name="c",
            version="1.0.0",
            description="",
            vendor="",
            license="",
            recipe_count=0,
            digest="",
            registry="r",
        )
        with patch.object(oci_router, "list_collections", return_value=[collection]):
            response = client.get("/api/oci/collections")
        assert response.json()[0]["display_version"] == "1.0.0"

    def test_an_unknown_registry_is_404(self, client):
        with patch.object(
            oci_router,
            "list_collections",
            side_effect=ValueError("Registry 'x' not found"),
        ):
            response = client.get("/api/oci/collections?registry=x")
        assert response.status_code == 404
        assert response.json()["detail"] == "Registry 'x' not found"

    def test_a_transport_failure_is_500(self, client):
        with patch.object(
            oci_router, "list_collections", side_effect=RuntimeError("oras died")
        ):
            response = client.get("/api/oci/collections")
        assert response.status_code == 500
        assert response.json()["detail"] == "oras died"


class TestCollectionRecipes:
    def test_recipes_are_serialised_with_placement_flags(self, client):
        recipe = CollectionRecipe(
            name="spark-vllm-7b",
            description="Llama 3.1 8B",
            model="meta-llama/Llama-3.1-8B-Instruct",
            container="vllm-node",
            recipe_version="1.0.0",
            solo_only=True,
        )
        with patch.object(
            oci_router, "list_collection_recipes", return_value=[recipe]
        ) as lister:
            response = client.get(
                "/api/oci/collections/spark-recipes/recipes?version=1.0.0&registry=ghcr"
            )
        assert response.status_code == 200
        assert response.json() == [
            {
                "name": "spark-vllm-7b",
                "description": "Llama 3.1 8B",
                "model": "meta-llama/Llama-3.1-8B-Instruct",
                "container": "vllm-node",
                "recipe_version": "1.0.0",
                "solo_only": True,
                "cluster_only": False,
            }
        ]
        assert lister.call_args == call(
            collection_name="spark-recipes", version="1.0.0", registry_name="ghcr"
        )

    def test_empty_optional_fields_become_empty_strings(self, client):
        recipe = CollectionRecipe(
            name="bare", description="", model="", container="", recipe_version=""
        )
        with patch.object(oci_router, "list_collection_recipes", return_value=[recipe]):
            response = client.get("/api/oci/collections/c/recipes")
        assert response.json() == [
            {
                "name": "bare",
                "description": "",
                "model": "",
                "container": "",
                "recipe_version": "",
                "solo_only": False,
                "cluster_only": False,
            }
        ]

    def test_an_unknown_collection_is_404(self, client):
        with patch.object(
            oci_router,
            "list_collection_recipes",
            side_effect=ValueError("no such collection"),
        ):
            response = client.get("/api/oci/collections/ghost/recipes")
        assert response.status_code == 404
        assert response.json()["detail"] == "no such collection"

    def test_a_transport_failure_is_500(self, client):
        with patch.object(
            oci_router, "list_collection_recipes", side_effect=RuntimeError("timeout")
        ):
            response = client.get("/api/oci/collections/c/recipes")
        assert response.status_code == 500
        assert response.json()["detail"] == "timeout"


# ── Install ──────────────────────────────────────────────────────────────────


class TestInstallCollection:
    def test_name_and_version_are_both_required(self, client):
        with patch.object(oci_router, "install_collection") as install:
            response = client.post("/api/oci/install", json={"name": "c"})
        assert response.status_code == 400
        assert response.json()["detail"] == "name and version are required"
        install.assert_not_called()

    def test_installed_filenames_come_back(self, client):
        with patch.object(
            oci_router, "install_collection", return_value=["a.yaml", "b.yaml"]
        ) as install:
            response = client.post(
                "/api/oci/install",
                json={"name": "c", "version": "1.0.0", "registry": "ghcr"},
            )
        assert response.status_code == 200
        assert response.json() == {"installed": ["a.yaml", "b.yaml"]}
        assert install.call_args == call(
            name="c", version="1.0.0", registry_name="ghcr"
        )

    def test_an_unknown_collection_is_404(self, client):
        with patch.object(
            oci_router, "install_collection", side_effect=ValueError("nope")
        ):
            response = client.post(
                "/api/oci/install", json={"name": "c", "version": "1"}
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "nope"

    def test_a_pull_failure_is_500(self, client):
        with patch.object(
            oci_router, "install_collection", side_effect=OSError("disk full")
        ):
            response = client.post(
                "/api/oci/install", json={"name": "c", "version": "1"}
            )
        assert response.status_code == 500
        assert response.json()["detail"] == "disk full"


class TestInstallSingleRecipe:
    def test_collection_and_recipe_are_both_required(self, client):
        with patch.object(oci_router, "install_oci_recipe") as install:
            response = client.post("/api/oci/recipes/install", json={"collection": "c"})
        assert response.status_code == 400
        assert response.json()["detail"] == "collection and recipe are required"
        install.assert_not_called()

    def test_a_missing_version_is_passed_as_an_empty_tag(self, client):
        result = {"success": True, "recipe": "r", "action": "installed"}
        with patch.object(
            oci_router, "install_oci_recipe", return_value=result
        ) as install:
            response = client.post(
                "/api/oci/recipes/install", json={"collection": "c", "recipe": "r"}
            )
        assert response.status_code == 200
        assert response.json() == result
        assert install.call_args == call(
            collection_name="c",
            recipe_name="r",
            version="",
            registry_name=None,
            overwrite=False,
        )

    def test_overwrite_and_registry_are_forwarded(self, client):
        with patch.object(
            oci_router, "install_oci_recipe", return_value={"success": True}
        ) as install:
            response = client.post(
                "/api/oci/recipes/install",
                json={
                    "collection": "c",
                    "recipe": "r",
                    "version": "2.0.0",
                    "registry": "ghcr",
                    "overwrite": True,
                },
            )
        assert response.status_code == 200
        assert install.call_args.kwargs["overwrite"] is True
        assert install.call_args.kwargs["registry_name"] == "ghcr"
        assert install.call_args.kwargs["version"] == "2.0.0"

    def test_an_unknown_recipe_is_404(self, client):
        with patch.object(
            oci_router, "install_oci_recipe", side_effect=ValueError("no recipe")
        ):
            response = client.post(
                "/api/oci/recipes/install", json={"collection": "c", "recipe": "r"}
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "no recipe"

    def test_a_pull_failure_is_500(self, client):
        with patch.object(
            oci_router, "install_oci_recipe", side_effect=RuntimeError("boom")
        ):
            response = client.post(
                "/api/oci/recipes/install", json={"collection": "c", "recipe": "r"}
            )
        assert response.status_code == 500
        assert response.json()["detail"] == "boom"


class TestUpdateSingleRecipe:
    def test_collection_is_required(self, client):
        with patch.object(oci_router, "update_oci_recipe") as update:
            response = client.post("/api/oci/recipes/update/r.yaml", json={})
        assert response.status_code == 400
        assert response.json()["detail"] == "collection is required"
        update.assert_not_called()

    def test_the_recipe_name_comes_from_the_path(self, client):
        result = {"success": True, "recipe": "r.yaml", "action": "updated"}
        with patch.object(
            oci_router, "update_oci_recipe", return_value=result
        ) as update:
            response = client.post(
                "/api/oci/recipes/update/r.yaml",
                json={"collection": "c", "version": "2.0.0", "registry": "ghcr"},
            )
        assert response.status_code == 200
        assert response.json() == result
        assert update.call_args == call(
            recipe_name="r.yaml",
            collection_name="c",
            version="2.0.0",
            registry_name="ghcr",
        )

    def test_an_unknown_recipe_is_404(self, client):
        with patch.object(
            oci_router, "update_oci_recipe", side_effect=ValueError("gone")
        ):
            response = client.post(
                "/api/oci/recipes/update/r.yaml", json={"collection": "c"}
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "gone"

    def test_a_pull_failure_is_500(self, client):
        with patch.object(
            oci_router, "update_oci_recipe", side_effect=RuntimeError("boom")
        ):
            response = client.post(
                "/api/oci/recipes/update/r.yaml", json={"collection": "c"}
            )
        assert response.status_code == 500
        assert response.json()["detail"] == "boom"


class TestUninstallRecipe:
    def test_a_successful_uninstall_returns_the_tool_result(self, client):
        result = {"success": True, "recipe": "r.yaml"}
        with patch.object(
            oci_router, "uninstall_oci_recipe", return_value=result
        ) as uninstall:
            response = client.delete("/api/oci/recipes/r.yaml")
        assert response.status_code == 200
        assert response.json() == result
        assert uninstall.call_args == call("r.yaml")

    def test_an_unknown_recipe_is_404(self, client):
        with patch.object(
            oci_router, "uninstall_oci_recipe", return_value={"success": False}
        ):
            response = client.delete("/api/oci/recipes/ghost.yaml")
        assert response.status_code == 404
        assert response.json()["detail"] == "Recipe 'ghost.yaml' not found"


# ── Update check / apply ─────────────────────────────────────────────────────


class TestUpdateCheck:
    def test_updates_are_serialised_field_by_field(self, client):
        update = UpdateInfo(
            collection="spark-recipes",
            current_version="1.0.0",
            latest_version="1.1.0",
            current_digest="sha256:old",
            latest_digest="sha256:new",
            local_changes=True,
            added_recipes=["new.yaml"],
            modified_recipes=["changed.yaml"],
        )
        with patch.object(oci_router, "check_updates", return_value=[update]) as check:
            response = client.get(
                "/api/oci/check?collection=spark-recipes&registry=ghcr"
            )
        assert response.status_code == 200
        assert response.json() == [
            {
                "collection": "spark-recipes",
                "current_version": "1.0.0",
                "latest_version": "1.1.0",
                "current_digest": "sha256:old",
                "latest_digest": "sha256:new",
                "local_changes": True,
                "added_recipes": ["new.yaml"],
                "modified_recipes": ["changed.yaml"],
            }
        ]
        assert check.call_args == call(collection="spark-recipes", registry="ghcr")

    def test_a_registry_failure_is_500(self, client):
        with patch.object(
            oci_router, "check_updates", side_effect=RuntimeError("no network")
        ):
            response = client.get("/api/oci/check")
        assert response.status_code == 500
        assert response.json()["detail"] == "no network"


class TestApplyUpdates:
    def test_updates_and_the_overwrite_flag_are_forwarded(self, client):
        updates = [{"collection": "c", "target_version": "1.1.0", "registry": "ghcr"}]
        results = [{"success": True, "collection": "c", "installed": ["a.yaml"]}]
        with patch.object(oci_router, "apply_updates", return_value=results) as apply:
            response = client.post(
                "/api/oci/update", json={"updates": updates, "overwrite_local": True}
            )
        assert response.status_code == 200
        assert response.json() == results
        assert apply.call_args == call(updates, overwrite_local=True)

    def test_a_failure_mid_apply_is_500(self, client):
        with patch.object(
            oci_router, "apply_updates", side_effect=RuntimeError("half done")
        ):
            response = client.post(
                "/api/oci/update", json={"updates": [{"collection": "c"}]}
            )
        assert response.status_code == 500
        assert response.json()["detail"] == "half done"


# ── Metadata ─────────────────────────────────────────────────────────────────


class TestMetadata:
    def test_installed_recipe_metadata_is_serialised(self, client):
        with patch.object(oci_router, "list_oci_recipes", return_value=[_meta()]):
            response = client.get("/api/oci/recipes/meta")
        assert response.status_code == 200
        assert response.json() == [
            {
                "name": "spark-vllm-7b.yaml",
                "source": "spark-official",
                "collection": "spark-recipes",
                "version": "1.0.0",
                "digest": "sha256:abc123",
                "installed_at": "2026-06-15T02:00:00Z",
                "updated_at": "2026-06-16T02:00:00Z",
                "local_changes": False,
            }
        ]

    def test_one_recipe_metadata_record_is_serialised(self, client):
        with patch.object(
            oci_router, "get_oci_meta", return_value=_meta(local_changes=True)
        ) as getter:
            response = client.get("/api/oci/recipes/meta/spark-vllm-7b.yaml")
        assert response.status_code == 200
        assert response.json()["local_changes"] is True
        assert response.json()["collection"] == "spark-recipes"
        assert getter.call_args == call("spark-vllm-7b.yaml")

    def test_a_recipe_without_metadata_is_404(self, client):
        with patch.object(oci_router, "get_oci_meta", return_value=None):
            response = client.get("/api/oci/recipes/meta/handwritten.yaml")
        assert response.status_code == 404
        assert (
            response.json()["detail"] == "No OCI metadata found for 'handwritten.yaml'"
        )


# ── Auto-update, cache, background updater ───────────────────────────────────


class TestAutoUpdate:
    def test_settings_report_the_configured_values(self, client, monkeypatch):
        from spark_pulse.config import config

        monkeypatch.setitem(config._data, "oci_auto_update_enabled", True)
        monkeypatch.setitem(config._data, "oci_auto_update_schedule", "15 3 * * *")
        monkeypatch.setitem(config._data, "oci_auto_update_overwrite_local", True)

        response = client.get("/api/oci/auto-update/settings")
        assert response.status_code == 200
        assert response.json() == {
            "enabled": True,
            "schedule": "15 3 * * *",
            "overwrite_local": True,
        }

    def test_settings_can_be_updated_one_key_at_a_time(self, client, monkeypatch):
        from spark_pulse.config import config

        monkeypatch.setitem(config._data, "oci_auto_update_enabled", False)
        monkeypatch.setitem(config._data, "oci_auto_update_schedule", "0 2 * * *")
        monkeypatch.setitem(config._data, "oci_auto_update_overwrite_local", False)

        response = client.put(
            "/api/oci/auto-update/settings", json={"overwrite_local": True}
        )
        assert response.status_code == 200
        assert response.json() == {
            "enabled": False,
            "schedule": "0 2 * * *",
            "overwrite_local": True,
        }
        assert config.oci_auto_update_overwrite_local is True

    def test_a_manual_run_persists_its_log(self, client):
        result = {"updated": 1, "log": ["updated spark-recipes"]}
        with patch.object(oci_router, "run_auto_update", return_value=result) as run:
            with patch.object(oci_router, "save_auto_update_log") as save:
                response = client.post("/api/oci/auto-update/run")
        assert response.status_code == 200
        assert response.json() == result
        run.assert_called_once_with()
        save.assert_called_once_with()

    def test_a_failed_run_is_500_and_writes_no_log(self, client):
        with patch.object(
            oci_router, "run_auto_update", side_effect=RuntimeError("boom")
        ):
            with patch.object(oci_router, "save_auto_update_log") as save:
                response = client.post("/api/oci/auto-update/run")
        assert response.status_code == 500
        assert response.json()["detail"] == "boom"
        save.assert_not_called()


class TestCacheAndBackgroundUpdater:
    def test_clearing_the_whole_cache_passes_no_key(self, client):
        with patch.object(
            oci_router, "clear_oci_cache", return_value={"cleared": 4}
        ) as clear:
            response = client.post("/api/oci/cache/clear", json={})
        assert response.status_code == 200
        assert response.json() == {"cleared": 4}
        assert clear.call_args == call(None)

    def test_clearing_one_entry_passes_its_key(self, client):
        with patch.object(
            oci_router, "clear_oci_cache", return_value={"cleared": 1}
        ) as clear:
            response = client.post(
                "/api/oci/cache/clear", json={"key": "collections:ghcr.io/x"}
            )
        assert response.status_code == 200
        assert clear.call_args == call("collections:ghcr.io/x")

    def test_the_background_updater_can_be_started(self, client):
        with patch.object(oci_router, "start_background_updater") as start:
            response = client.post("/api/oci/background/start")
        assert response.status_code == 200
        assert response.json() == {"started": True}
        start.assert_called_once_with()

    def test_the_background_updater_can_be_stopped(self, client):
        with patch.object(oci_router, "stop_background_updater") as stop:
            response = client.post("/api/oci/background/stop")
        assert response.status_code == 200
        assert response.json() == {"stopped": True}
        stop.assert_called_once_with()


# ── Simulation-only branches ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_mock_recipe_state():
    """`_mock_installed_recipes` is process-wide; keep it per-test."""
    oci_router._mock_installed_recipes.clear()
    yield
    oci_router._mock_installed_recipes.clear()


class TestSimulationBranches:
    def test_collections_can_be_filtered_by_version(self, sim_client):
        response = sim_client.get("/api/oci/collections?version=0.3.0")
        assert response.status_code == 200
        assert [c["name"] for c in response.json()] == ["community-recipes"]

    def test_registry_versions_are_canned(self, sim_client):
        response = sim_client.get("/api/oci/registries/any-registry/versions")
        assert response.status_code == 200
        assert response.json() == {"versions": ["1.0.0", "1.0.1", "latest"]}

    def test_installing_an_unknown_collection_version_is_404(self, sim_client):
        response = sim_client.post(
            "/api/oci/install", json={"name": "spark-recipes", "version": "9.9.9"}
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Collection 'spark-recipes:9.9.9' not found"

    def test_a_known_collection_lists_its_recipes(self, sim_client):
        response = sim_client.get("/api/oci/collections/community-recipes/recipes")
        assert response.status_code == 200
        assert [r["name"] for r in response.json()] == [
            "community-llama-3-8b",
            "community-mixtral-8x7b",
            "community-qwen-72b",
        ]

    def test_an_unknown_collection_has_no_recipes(self, sim_client):
        response = sim_client.get("/api/oci/collections/ghost/recipes")
        assert response.status_code == 200
        assert response.json() == []

    def test_installing_a_recipe_twice_is_idempotent(self, sim_client):
        body = {"collection": "spark-recipes", "recipe": "spark-vllm-7b"}
        first = sim_client.post("/api/oci/recipes/install", json=body)
        second = sim_client.post("/api/oci/recipes/install", json=body)
        assert first.json() == {
            "success": True,
            "recipe": "spark-vllm-7b",
            "action": "installed",
        }
        assert second.json()["action"] == "up_to_date"

    def test_updating_an_installed_recipe_reports_updated(self, sim_client):
        sim_client.post(
            "/api/oci/recipes/install",
            json={"collection": "spark-recipes", "recipe": "spark-vllm-7b"},
        )
        response = sim_client.post(
            "/api/oci/recipes/update/spark-vllm-7b",
            json={"collection": "spark-recipes"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "recipe": "spark-vllm-7b",
            "action": "updated",
        }

    def test_updating_a_recipe_that_was_never_installed_is_404(self, sim_client):
        response = sim_client.post(
            "/api/oci/recipes/update/spark-vllm-7b",
            json={"collection": "spark-recipes"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Recipe 'spark-vllm-7b' is not installed"
