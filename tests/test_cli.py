"""Behaviour of the `spark-pulse` Click CLI.

Every external effect is mocked at its boundary: ``os.execvp`` for ``start``,
the ``spark_pulse.service`` helpers for the systemd commands, and
``spark_pulse.tools.oci_registry`` for the ``recipes``/``oci`` groups. Nothing
here starts a server, talks to systemd, or reaches the network.

`tests/test_cli_recipes_validate.py` owns `spark-pulse recipes validate`.
"""

from __future__ import annotations

import json
import os
import sys
import types
from unittest.mock import Mock, call, patch

import pytest
from click.testing import CliRunner

from spark_pulse import cli
from spark_pulse.cli import main
from spark_pulse.tools.oci_registry import (
    CollectionInfo,
    UpdateInfo,
)

OCI = "spark_pulse.tools.oci_registry"


@pytest.fixture
def runner():
    return CliRunner()


def _collection(name="spark-recipes", version="1.0.0", **kw):
    fields = {
        "name": name,
        "version": version,
        "description": "Spark Pulse recipe collection",
        "vendor": "Kharkevich Engineering Lab",
        "license": "MIT",
        "recipe_count": 5,
        "digest": "sha256:abc123",
        "registry": "ghcr.io/example/recipes",
    }
    fields.update(kw)
    return CollectionInfo(**fields)


# ── start ────────────────────────────────────────────────────────────────────


class TestStart:
    def test_dry_run_prints_the_command_and_does_not_exec(self, runner):
        with patch.object(cli.os, "execvp") as execvp:
            result = runner.invoke(
                main, ["start", "--host", "127.0.0.1", "--port", "9101", "--dry-run"]
            )
        assert result.exit_code == 0
        assert (
            "Would run: uvicorn spark_pulse.app:app --host 127.0.0.1 "
            "--port 9101 --workers 1" in result.output
        )
        execvp.assert_not_called()

    def test_execs_uvicorn_with_the_requested_bind_and_workers(self, runner):
        with patch.object(cli.os, "execvp") as execvp:
            result = runner.invoke(
                main,
                [
                    "start",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "9999",
                    "--workers",
                    "3",
                ],
            )
        assert result.exit_code == 0
        assert execvp.call_args == call(
            "uvicorn",
            [
                "uvicorn",
                "spark_pulse.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                "9999",
                "--workers",
                "3",
            ],
        )

    def test_env_file_populates_the_environment_uvicorn_inherits(
        self, runner, tmp_path, monkeypatch
    ):
        env_file = tmp_path / "app.env"
        env_file.write_text(
            "# a comment\n"
            "\n"
            "SPARK_VLLM_PATH=/opt/spark-vllm-docker\n"
            "NO_EQUALS_SIGN\n"
            "  PADDED = spaced value \n",
            encoding="utf-8",
        )
        fake_env = dict(os.environ)
        monkeypatch.setattr(cli.os, "environ", fake_env)

        with patch.object(cli.os, "execvp") as execvp:
            result = runner.invoke(
                main, ["start", "--env-file", str(env_file), "--dry-run"]
            )

        assert result.exit_code == 0
        assert fake_env["SPARK_VLLM_PATH"] == "/opt/spark-vllm-docker"
        assert fake_env["PADDED"] == "spaced value"
        assert "NO_EQUALS_SIGN" not in fake_env
        execvp.assert_not_called()

    def test_missing_env_file_warns_but_still_starts(self, runner, tmp_path):
        missing = tmp_path / "nope.env"
        with patch.object(cli.os, "execvp") as execvp:
            result = runner.invoke(
                main, ["start", "--env-file", str(missing), "--dry-run"]
            )
        assert result.exit_code == 0
        assert f"Warning: env file {missing} not found." in result.stderr
        assert "Would run: uvicorn" in result.stdout
        execvp.assert_not_called()


# ── install / uninstall ──────────────────────────────────────────────────────


class TestInstall:
    def test_installs_and_starts_a_system_unit_by_default(self, runner):
        with patch.object(cli, "install_systemd") as install:
            result = runner.invoke(
                main,
                [
                    "install",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8123",
                    "--service-user",
                    "alice",
                ],
            )
        assert result.exit_code == 0
        assert install.call_args == call(
            host="127.0.0.1", port=8123, user="alice", start=True, scope="system"
        )
        assert f"Installed systemd service '{cli.SERVICE_NAME}'." in result.output
        assert "Service started." in result.output
        assert "loginctl" not in result.output

    def test_no_start_installs_without_starting_and_says_how_to_start(self, runner):
        with patch.object(cli, "install_systemd") as install:
            result = runner.invoke(main, ["install", "--no-start"])
        assert result.exit_code == 0
        assert install.call_args.kwargs["start"] is False
        assert "Run 'spark-pulse start-service' to start." in result.output
        assert "Service started." not in result.output

    def test_user_scope_installs_a_user_unit_and_mentions_linger(self, runner):
        with patch.object(cli, "install_systemd") as install:
            result = runner.invoke(main, ["install", "--user"])
        assert result.exit_code == 0
        assert install.call_args.kwargs["scope"] == "user"
        assert "loginctl enable-linger $USER" in result.output

    def test_permission_error_on_system_scope_asks_for_root(self, runner):
        with patch.object(cli, "install_systemd", side_effect=PermissionError):
            result = runner.invoke(main, ["install"])
        assert result.exit_code == 1
        assert "requires root/sudo privileges" in result.stderr

    def test_permission_error_on_user_scope_points_at_config_permissions(self, runner):
        with patch.object(cli, "install_systemd", side_effect=PermissionError):
            result = runner.invoke(main, ["install", "--user"])
        assert result.exit_code == 1
        assert "unable to write user systemd files" in result.stderr


class TestUninstall:
    def test_removes_the_system_unit(self, runner):
        with patch.object(cli, "uninstall_systemd") as uninstall:
            result = runner.invoke(main, ["uninstall"])
        assert result.exit_code == 0
        assert uninstall.call_args == call(scope="system")
        assert f"Removed systemd service '{cli.SERVICE_NAME}'." in result.output

    def test_user_flag_selects_the_user_scope(self, runner):
        with patch.object(cli, "uninstall_systemd") as uninstall:
            result = runner.invoke(main, ["uninstall", "--user"])
        assert result.exit_code == 0
        assert uninstall.call_args == call(scope="user")

    def test_permission_error_on_system_scope_asks_for_root(self, runner):
        with patch.object(cli, "uninstall_systemd", side_effect=PermissionError):
            result = runner.invoke(main, ["uninstall"])
        assert result.exit_code == 1
        assert "requires root/sudo privileges" in result.stderr

    def test_permission_error_on_user_scope_points_at_config_permissions(self, runner):
        with patch.object(cli, "uninstall_systemd", side_effect=PermissionError):
            result = runner.invoke(main, ["uninstall", "--user"])
        assert result.exit_code == 1
        assert "unable to remove user systemd files" in result.stderr


# ── status / start-service / stop-service ────────────────────────────────────


class TestStatus:
    @pytest.mark.parametrize(
        "state,expected",
        [
            ("active", "active (running)"),
            ("inactive", "inactive"),
            ("dead", "inactive"),
            ("not-installed", "not available (systemd required)"),
            ("unknown-to-us", "not installed"),
        ],
    )
    def test_reports_each_systemd_state_and_exits_zero(self, runner, state, expected):
        with patch.object(cli, "get_status", return_value=state) as get_status:
            result = runner.invoke(main, ["status"])
        assert result.exit_code == 0
        assert get_status.call_args == call(scope="system")
        assert result.output.strip() == f"{cli.SERVICE_NAME}: {expected}"

    def test_failed_state_exits_non_zero_on_stderr(self, runner):
        with patch.object(cli, "get_status", return_value="failed"):
            result = runner.invoke(main, ["status"])
        assert result.exit_code == 1
        assert f"{cli.SERVICE_NAME}: failed" in result.stderr

    def test_user_flag_queries_the_user_scope(self, runner):
        with patch.object(cli, "get_status", return_value="active") as get_status:
            result = runner.invoke(main, ["status", "--user"])
        assert result.exit_code == 0
        assert get_status.call_args == call(scope="user")


class TestServiceStartStop:
    @pytest.mark.parametrize("args,scope", [([], "system"), (["--user"], "user")])
    def test_start_service_delegates_with_the_right_scope(self, runner, args, scope):
        with patch.object(cli, "start_server") as start_server:
            result = runner.invoke(main, ["start-service", *args])
        assert result.exit_code == 0
        assert start_server.call_args == call(scope=scope)

    @pytest.mark.parametrize("args,scope", [([], "system"), (["--user"], "user")])
    def test_stop_service_delegates_with_the_right_scope(self, runner, args, scope):
        with patch.object(cli, "stop_server") as stop_server:
            result = runner.invoke(main, ["stop-service", *args])
        assert result.exit_code == 0
        assert stop_server.call_args == call(scope=scope)


# ── mcp ──────────────────────────────────────────────────────────────────────


def test_mcp_hands_control_to_the_stdio_server(runner, monkeypatch):
    fake = types.ModuleType("spark_pulse.mcp_server")
    fake.main = Mock()
    monkeypatch.setitem(sys.modules, "spark_pulse.mcp_server", fake)
    result = runner.invoke(main, ["mcp"])
    assert result.exit_code == 0
    fake.main.assert_called_once_with()


# ── recipes validate (the empty-target case) ─────────────────────────────────


def test_recipes_validate_on_an_empty_directory_says_so(runner, tmp_path):
    result = runner.invoke(main, ["recipes", "validate", str(tmp_path)])
    assert result.exit_code == 0
    assert "No recipe files found." in result.output
    assert "0 valid, 0 invalid." in result.output


# ── recipes list ─────────────────────────────────────────────────────────────


class TestRecipesList:
    def test_empty_result_points_at_the_add_registry_command(self, runner):
        with patch(f"{OCI}.list_collections", return_value=[]):
            result = runner.invoke(main, ["recipes", "list"])
        assert result.exit_code == 0
        assert "No collections found." in result.output
        assert "spark-pulse oci add" in result.output

    def test_table_output_shows_every_collection_and_passes_the_filters(self, runner):
        collections = [
            _collection(),
            _collection(name="community", version="0.3.0", recipe_count=3),
        ]
        with patch(
            f"{OCI}.list_collections", return_value=collections
        ) as list_collections:
            result = runner.invoke(
                main, ["recipes", "list", "--registry", "ghcr", "--version", "1.0.0"]
            )
        assert result.exit_code == 0
        assert list_collections.call_args == call(registry_name="ghcr", version="1.0.0")
        assert "Collection" in result.output
        assert "spark-recipes" in result.output
        assert "community" in result.output
        assert "Kharkevich Enginee" in result.output  # vendor truncated to 18 chars

    def test_json_output_is_machine_readable(self, runner):
        with patch(f"{OCI}.list_collections", return_value=[_collection()]):
            result = runner.invoke(main, ["recipes", "list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == [
            {
                "name": "spark-recipes",
                "version": "1.0.0",
                "description": "Spark Pulse recipe collection",
                "vendor": "Kharkevich Engineering Lab",
                "license": "MIT",
                "recipe_count": 5,
                "digest": "sha256:abc123",
                "registry": "ghcr.io/example/recipes",
            }
        ]

    def test_a_bad_registry_name_exits_one(self, runner):
        with patch(
            f"{OCI}.list_collections", side_effect=ValueError("no such registry")
        ):
            result = runner.invoke(main, ["recipes", "list", "--registry", "nope"])
        assert result.exit_code == 1
        assert "Error: no such registry" in result.stderr

    def test_a_transport_failure_exits_one(self, runner):
        with patch(
            f"{OCI}.list_collections", side_effect=RuntimeError("oras exploded")
        ):
            result = runner.invoke(main, ["recipes", "list"])
        assert result.exit_code == 1
        assert "Error listing collections: oras exploded" in result.stderr


# ── recipes install ──────────────────────────────────────────────────────────


class TestRecipesInstall:
    def test_installs_the_latest_version_by_default_and_lists_the_files(self, runner):
        with patch(
            f"{OCI}.install_collection", return_value=["a.yaml", "b.yaml"]
        ) as install:
            result = runner.invoke(main, ["recipes", "install", "spark-recipes"])
        assert result.exit_code == 0
        assert install.call_args == call(
            name="spark-recipes", version="latest", registry_name=None, dry_run=False
        )
        assert "Installed 2 recipe(s) from spark-recipes:" in result.output
        assert "a.yaml" in result.output and "b.yaml" in result.output

    def test_explicit_version_and_registry_are_forwarded(self, runner):
        with patch(f"{OCI}.install_collection", return_value=["a.yaml"]) as install:
            result = runner.invoke(
                main,
                ["recipes", "install", "c", "--version", "2.1.0", "--registry", "ghcr"],
            )
        assert result.exit_code == 0
        assert install.call_args.kwargs["version"] == "2.1.0"
        assert install.call_args.kwargs["registry_name"] == "ghcr"

    def test_dry_run_reports_intent_without_listing_files(self, runner):
        with patch(f"{OCI}.install_collection", return_value=[]) as install:
            result = runner.invoke(main, ["recipes", "install", "c", "--dry-run"])
        assert result.exit_code == 0
        assert install.call_args.kwargs["dry_run"] is True
        assert "DRY RUN: Would install collection 'c'" in result.output

    def test_an_empty_collection_says_nothing_was_found(self, runner):
        with patch(f"{OCI}.install_collection", return_value=[]):
            result = runner.invoke(main, ["recipes", "install", "empty-coll"])
        assert result.exit_code == 0
        assert "No recipes found in collection 'empty-coll'." in result.output

    def test_unknown_collection_exits_one(self, runner):
        with patch(f"{OCI}.install_collection", side_effect=ValueError("not found")):
            result = runner.invoke(main, ["recipes", "install", "ghost"])
        assert result.exit_code == 1
        assert "Error: not found" in result.stderr

    def test_transport_failure_exits_one(self, runner):
        with patch(f"{OCI}.install_collection", side_effect=OSError("disk full")):
            result = runner.invoke(main, ["recipes", "install", "c"])
        assert result.exit_code == 1
        assert "Error installing collection: disk full" in result.stderr


# ── recipes update ───────────────────────────────────────────────────────────


def _update(collection="spark-recipes", local_changes=False, **kw):
    fields = {
        "collection": collection,
        "current_version": "1.0.0",
        "latest_version": "1.1.0",
        "current_digest": "sha256:old",
        "latest_digest": "sha256:new",
        "local_changes": local_changes,
        "added_recipes": ["new.yaml"],
        "modified_recipes": ["changed.yaml"],
    }
    fields.update(kw)
    return UpdateInfo(**fields)


class TestRecipesUpdate:
    def test_no_updates_available(self, runner):
        with patch(f"{OCI}.check_updates", return_value=[]) as check:
            with patch(f"{OCI}.apply_updates") as apply:
                result = runner.invoke(main, ["recipes", "update", "--all"])
        assert result.exit_code == 0
        assert check.call_args == call(collection=None, registry=None)
        assert "No updates available." in result.output
        apply.assert_not_called()

    def test_check_failure_exits_one(self, runner):
        with patch(f"{OCI}.check_updates", side_effect=RuntimeError("registry down")):
            result = runner.invoke(main, ["recipes", "update"])
        assert result.exit_code == 1
        assert "Error checking updates: registry down" in result.stderr

    def test_dry_run_lists_the_diff_without_applying(self, runner):
        with patch(f"{OCI}.check_updates", return_value=[_update()]):
            with patch(f"{OCI}.apply_updates") as apply:
                result = runner.invoke(
                    main,
                    ["recipes", "update", "--collection", "spark-recipes", "--dry-run"],
                )
        assert result.exit_code == 0
        assert "spark-recipes: 1.0.0 → 1.1.0" in result.output
        assert "Added: new.yaml" in result.output
        assert "Modified: changed.yaml" in result.output
        apply.assert_not_called()

    def test_applies_updates_and_reports_each_result(self, runner):
        results = [
            {
                "success": True,
                "collection": "spark-recipes",
                "installed": ["a.yaml", "b.yaml"],
            },
            {"success": False, "collection": "other", "error": "digest mismatch"},
        ]
        with patch(f"{OCI}.check_updates", return_value=[_update(), _update("other")]):
            with patch(f"{OCI}.apply_updates", return_value=results) as apply:
                result = runner.invoke(
                    main, ["recipes", "update", "--registry", "ghcr"]
                )
        assert result.exit_code == 0
        assert apply.call_args == call(
            [
                {
                    "collection": "spark-recipes",
                    "target_version": "1.1.0",
                    "registry": "ghcr",
                },
                {"collection": "other", "target_version": "1.1.0", "registry": "ghcr"},
            ]
        )
        assert "✓ Updated spark-recipes: 2 recipes" in result.output
        assert "✗ Failed to update other: digest mismatch" in result.stderr

    def test_collections_with_local_changes_are_skipped(self, runner):
        with patch(f"{OCI}.check_updates", return_value=[_update(local_changes=True)]):
            with patch(f"{OCI}.apply_updates") as apply:
                result = runner.invoke(main, ["recipes", "update"])
        assert result.exit_code == 0
        assert "⚠ spark-recipes" in result.output
        assert "Skipping spark-recipes: local changes detected" in result.output
        assert "No updates to apply." in result.output
        apply.assert_not_called()

    def test_apply_failure_exits_one(self, runner):
        with patch(f"{OCI}.check_updates", return_value=[_update()]):
            with patch(f"{OCI}.apply_updates", side_effect=RuntimeError("boom")):
                result = runner.invoke(main, ["recipes", "update"])
        assert result.exit_code == 1
        assert "Error applying updates: boom" in result.stderr


# ── recipes auto-update ──────────────────────────────────────────────────────


class TestRecipesAutoUpdate:
    def test_enable_sets_the_config_flag(self, runner, monkeypatch):
        from spark_pulse.config import config

        monkeypatch.setitem(config._data, "oci_auto_update_enabled", False)
        with patch(f"{OCI}.run_auto_update") as run:
            result = runner.invoke(main, ["recipes", "auto-update", "--enable"])
        assert result.exit_code == 0
        assert "Auto-update enabled." in result.output
        assert config.oci_auto_update_enabled is True
        run.assert_not_called()

    def test_disable_clears_the_config_flag(self, runner, monkeypatch):
        from spark_pulse.config import config

        monkeypatch.setitem(config._data, "oci_auto_update_enabled", True)
        with patch(f"{OCI}.run_auto_update") as run:
            result = runner.invoke(main, ["recipes", "auto-update", "--disable"])
        assert result.exit_code == 0
        assert "Auto-update disabled." in result.output
        assert config.oci_auto_update_enabled is False
        run.assert_not_called()

    def test_a_skipped_run_reports_the_reason(self, runner):
        with patch(
            f"{OCI}.run_auto_update",
            return_value={"skipped": True, "reason": "disabled"},
        ):
            result = runner.invoke(main, ["recipes", "auto-update"])
        assert result.exit_code == 0
        assert "Auto-update skipped: disabled" in result.output

    def test_a_run_prints_the_log_and_the_count(self, runner, monkeypatch):
        from spark_pulse.config import config

        monkeypatch.setitem(config._data, "oci_auto_update_schedule", "0 2 * * *")
        payload = {
            "log": ["checking spark-recipes", "updated spark-recipes"],
            "updated": 2,
        }
        with patch(f"{OCI}.run_auto_update", return_value=payload):
            result = runner.invoke(
                main, ["recipes", "auto-update", "--schedule", "30 4 * * *"]
            )
        assert result.exit_code == 0
        assert "Schedule set to: 30 4 * * *" in result.output
        assert config.oci_auto_update_schedule == "30 4 * * *"
        assert "checking spark-recipes" in result.output
        assert "2 recipe(s) updated." in result.output

    def test_a_run_with_nothing_to_do_says_so(self, runner):
        with patch(f"{OCI}.run_auto_update", return_value={"log": [], "updated": 0}):
            result = runner.invoke(main, ["recipes", "auto-update"])
        assert result.exit_code == 0
        assert "No recipes updated." in result.output


# ── oci registry management ──────────────────────────────────────────────────


class TestOciRegistries:
    def test_add_registry_forwards_a_complete_record(self, runner):
        added = {"name": "mine", "url": "registry.example.com/recipes"}
        with patch(f"{OCI}.add_registry", return_value=added) as add:
            result = runner.invoke(
                main,
                [
                    "oci",
                    "add-registry",
                    "mine",
                    "registry.example.com/recipes",
                    "--default",
                ],
            )
        assert result.exit_code == 0
        assert add.call_args == call(
            {
                "name": "mine",
                "url": "registry.example.com/recipes",
                "enabled": True,
                "default": True,
                "auth": {},
            }
        )
        assert "Added registry 'mine' (registry.example.com/recipes)." in result.output

    def test_add_registry_without_default_flag(self, runner):
        with patch(
            f"{OCI}.add_registry", return_value={"name": "m", "url": "u"}
        ) as add:
            result = runner.invoke(main, ["oci", "add-registry", "m", "u"])
        assert result.exit_code == 0
        assert add.call_args[0][0]["default"] is False

    def test_add_registry_failure_exits_one(self, runner):
        with patch(f"{OCI}.add_registry", side_effect=ValueError("duplicate name")):
            result = runner.invoke(main, ["oci", "add-registry", "m", "u"])
        assert result.exit_code == 1
        assert "Error: duplicate name" in result.stderr

    def test_list_registries_when_none_configured(self, runner):
        with patch(f"{OCI}.list_registries", return_value=[]):
            result = runner.invoke(main, ["oci", "list-registries"])
        assert result.exit_code == 0
        assert "No registries configured." in result.output

    def test_list_registries_renders_flags_as_yes_no(self, runner):
        regs = [
            {
                "name": "spark-official",
                "url": "ghcr.io/example/recipes",
                "enabled": True,
                "default": True,
                "connected": True,
            },
            {"name": "offline", "enabled": False, "default": False, "connected": False},
        ]
        with patch(f"{OCI}.list_registries", return_value=regs):
            result = runner.invoke(main, ["oci", "list-registries"])
        assert result.exit_code == 0
        lines = result.output.splitlines()
        assert lines[0].startswith("Name")
        official = next(line for line in lines if line.startswith("spark-official"))
        assert official.split() == [
            "spark-official",
            "ghcr.io/example/recipes",
            "yes",
            "yes",
            "yes",
        ]
        offline = next(line for line in lines if line.startswith("offline"))
        assert offline.split() == ["offline", "no", "no", "no"]

    def test_remove_registry_reports_success(self, runner):
        with patch(f"{OCI}.remove_registry", return_value=True) as remove:
            result = runner.invoke(main, ["oci", "remove-registry", "mine"])
        assert result.exit_code == 0
        assert remove.call_args == call("mine")
        assert "Removed registry 'mine'." in result.output

    def test_remove_unknown_registry_exits_one(self, runner):
        with patch(f"{OCI}.remove_registry", return_value=False):
            result = runner.invoke(main, ["oci", "remove-registry", "ghost"])
        assert result.exit_code == 1
        assert "Registry 'ghost' not found." in result.stderr
