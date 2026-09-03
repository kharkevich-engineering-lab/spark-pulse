"""CLI: `spark-pulse manage start|stop|install|uninstall|status`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from spark_pulse.service import (
    SERVICE_NAME,
    install_systemd,
    uninstall_systemd,
    start_server,
    stop_server,
    get_status,
)
from spark_pulse.config import config


@click.group()
def main():
    """Spark Manager — Web UI for spark-vllm-docker."""
    pass


@main.command()
@click.option("--host", "host", default="0.0.0.0", help="Bind address")
@click.option("--port", "port", default=config.webui_port, type=int, help="Bind port")
@click.option("--workers", "workers", default=1, type=int, help="Number of workers")
@click.option(
    "--env-file",
    "env_file",
    default=None,
    type=click.Path(),
    help="Environment file path",
)
@click.option("--dry-run", is_flag=True, help="Show command without executing")
def start(host, port, workers, env_file, dry_run):
    """Start the Spark Manager web server."""
    if env_file:
        load_env(env_file)

    cmd = f"uvicorn spark_pulse.app:app --host {host} --port {port} --workers {workers}"

    if dry_run:
        click.echo(f"Would run: {cmd}")
        return

    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "spark_pulse.app:app",
            "--host",
            host,
            "--port",
            str(port),
            "--workers",
            str(workers),
        ],
    )


@main.command()
@click.option("--host", "host", default="0.0.0.0", help="Systemd service bind address")
@click.option(
    "--port",
    "port",
    default=config.webui_port,
    type=int,
    help="Systemd service bind port",
)
@click.option(
    "--service-user",
    "service_user",
    default=os.environ.get("USER", "spark"),
    help="System service Unix user",
)
@click.option(
    "--user", "use_user", is_flag=True, help="Install a user-scoped systemd service"
)
@click.option("--no-start", is_flag=True, help="Install but don't start")
def install(host, port, service_user, use_user, no_start):
    """Install and enable the systemd service."""
    scope = "user" if use_user else "system"
    try:
        install_systemd(
            host=host, port=port, user=service_user, start=not no_start, scope=scope
        )
        click.echo(f"Installed systemd service '{SERVICE_NAME}'.")
        if no_start:
            click.echo(
                "Service installed but not started. Run 'spark-pulse start-service' to start."
            )
        else:
            click.echo("Service started.")
        if use_user:
            click.echo(
                "User unit installed. To start at boot without login session, run: loginctl enable-linger $USER"
            )
    except PermissionError:
        if use_user:
            click.echo(
                "Error: unable to write user systemd files. Check ~/.config permissions.",
                err=True,
            )
        else:
            click.echo(
                "Error: systemd installation requires root/sudo privileges.", err=True
            )
        sys.exit(1)


@main.command()
@click.option(
    "--user", "use_user", is_flag=True, help="Uninstall user-scoped systemd service"
)
def uninstall(use_user):
    """Remove the systemd service."""
    scope = "user" if use_user else "system"
    try:
        uninstall_systemd(scope=scope)
        click.echo(f"Removed systemd service '{SERVICE_NAME}'.")
    except PermissionError:
        if use_user:
            click.echo(
                "Error: unable to remove user systemd files. Check ~/.config permissions.",
                err=True,
            )
        else:
            click.echo(
                "Error: systemd uninstall requires root/sudo privileges.", err=True
            )
        sys.exit(1)


@main.command()
@click.option(
    "--user", "use_user", is_flag=True, help="Check user-scoped systemd service status"
)
def status(use_user):
    """Check systemd service status."""
    scope = "user" if use_user else "system"
    status = get_status(scope=scope)
    if status == "active":
        click.echo(f"{SERVICE_NAME}: active (running)")
    elif status in ("inactive", "dead"):
        click.echo(f"{SERVICE_NAME}: inactive")
    elif status == "failed":
        click.echo(f"{SERVICE_NAME}: failed", err=True)
        sys.exit(1)
    elif status == "not-installed":
        click.echo(f"{SERVICE_NAME}: not available (systemd required)")
    else:
        click.echo(f"{SERVICE_NAME}: not installed")


@main.command()
@click.option(
    "--user", "use_user", is_flag=True, help="Stop user-scoped systemd service"
)
def stop_service(use_user):
    """Stop the systemd service."""
    scope = "user" if use_user else "system"
    stop_server(scope=scope)


@main.command()
@click.option(
    "--user", "use_user", is_flag=True, help="Start user-scoped systemd service"
)
def start_service(use_user):
    """Start the systemd service."""
    scope = "user" if use_user else "system"
    start_server(scope=scope)


@main.command()
def mcp():
    """Start the MCP (Model Context Protocol) server for AI assistants."""
    from spark_pulse.mcp_server import main as mcp_main

    mcp_main()


def load_env(env_file: str):
    """Load environment variables from a .env file."""
    path = Path(env_file)
    if not path.exists():
        click.echo(f"Warning: env file {env_file} not found.", err=True)
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()


# ── OCI Recipe Commands ──────────────────────────────────────────────────────


@main.group()
def recipes():
    """Manage OCI recipe collections."""
    pass


@recipes.command("validate")
@click.argument(
    "targets", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option("--json", "as_json", is_flag=True, help="Output results as JSON")
def recipes_validate(targets, as_json):
    """Validate recipe files against the published schema.

    TARGETS may be recipe files or directories (scanned recursively).
    Exits non-zero when any recipe fails validation.
    """
    from spark_pulse.tools.recipe_schema import (
        validate_recipe_dir,
        validate_recipe_file,
    )

    results = []
    for target in targets:
        if target.is_dir():
            results.extend(validate_recipe_dir(target))
        else:
            results.append(validate_recipe_file(target))

    if as_json:
        import json as _json

        click.echo(_json.dumps(results, indent=2))
    else:
        if not results:
            click.echo("No recipe files found.")
        for result in results:
            if result["ok"]:
                click.echo(
                    f"  OK    {result['path']} "
                    f"(v{result['recipe_version']}, {result['name']})"
                )
            else:
                click.echo(f"  FAIL  {result['path']}")
                for err in result["errors"]:
                    where = f"{err['path']}: " if err["path"] else ""
                    click.echo(f"          {where}{err['message']}")

    failed = [r for r in results if not r["ok"]]
    if not as_json:
        click.echo(f"\n{len(results) - len(failed)} valid, {len(failed)} invalid.")
    if failed:
        sys.exit(1)


@recipes.command("list")
@click.option("--registry", "registry", default=None, help="Registry name to filter by")
@click.option("--version", "version", default=None, help="Version tag to filter by")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--dry-run", is_flag=True, help="Show command without executing")
def recipes_list(registry, version, as_json, dry_run):
    """List available recipe collections from OCI registries."""
    from spark_pulse.tools.oci_registry import list_collections

    try:
        collections = list_collections(registry_name=registry, version=version)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error listing collections: {exc}", err=True)
        sys.exit(1)

    if not collections:
        click.echo(
            "No collections found. Configure registries with 'spark-pulse oci add'."
        )
        return

    if as_json:
        import json

        data = [
            {
                "name": c.name,
                "version": c.version,
                "description": c.description,
                "vendor": c.vendor,
                "license": c.license,
                "recipe_count": c.recipe_count,
                "digest": c.digest,
                "registry": c.registry,
            }
            for c in collections
        ]
        click.echo(json.dumps(data, indent=2))
        return

    # Table output
    click.echo(
        f"{'Collection':<25} {'Version':<10} {'Description':<35} {'Vendor':<20} {'Recipes':>8}"
    )
    click.echo("─" * 98)
    for c in collections:
        desc = (c.description or "")[:33]
        vendor = (c.vendor or "")[:18]
        click.echo(
            f"{c.name:<25} {c.version:<10} {desc:<35} {vendor:<20} {c.recipe_count:>8}"
        )


@recipes.command("install")
@click.argument("name")
@click.option(
    "--version", "version", default=None, help="Version tag (default: latest)"
)
@click.option("--registry", "registry", default=None, help="Registry name")
@click.option("--dry-run", is_flag=True, help="Show what would be installed")
def recipes_install(name, version, registry, dry_run):
    """Install a recipe collection from an OCI registry."""
    from spark_pulse.tools.oci_registry import install_collection

    try:
        installed = install_collection(
            name=name,
            version=version or "latest",
            registry_name=registry,
            dry_run=dry_run,
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error installing collection: {exc}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"DRY RUN: Would install collection '{name}'")
        return

    if not installed:
        click.echo(f"No recipes found in collection '{name}'.")
        return

    click.echo(f"Installed {len(installed)} recipe(s) from {name}:")
    for f in installed:
        click.echo(f"  ✓ {f}")


@recipes.command("update")
@click.option("--collection", "collection", default=None, help="Collection to update")
@click.option("--all", "update_all", is_flag=True, help="Update all collections")
@click.option("--registry", "registry", default=None, help="Registry to check")
@click.option("--dry-run", is_flag=True, help="Show available updates without applying")
def recipes_update(collection, update_all, registry, dry_run):
    """Check for and apply updates to installed OCI recipe collections."""
    from spark_pulse.tools.oci_registry import check_updates, apply_updates

    try:
        updates = check_updates(collection=collection, registry=registry)
    except Exception as exc:
        click.echo(f"Error checking updates: {exc}", err=True)
        sys.exit(1)

    if not updates:
        click.echo("No updates available.")
        return

    click.echo("Available updates:")
    for u in updates:
        status_icon = "⚠" if u.local_changes else "✓"
        click.echo(
            f"  {status_icon} {u.collection}: {u.current_version} → {u.latest_version}"
        )
        if u.added_recipes:
            click.echo(f"     Added: {', '.join(u.added_recipes)}")
        if u.modified_recipes:
            click.echo(f"     Modified: {', '.join(u.modified_recipes)}")

    if dry_run:
        return

    # Apply updates
    update_params = []
    for u in updates:
        if u.local_changes:
            click.echo(f"  Skipping {u.collection}: local changes detected")
            continue
        update_params.append(
            {
                "collection": u.collection,
                "target_version": u.latest_version,
                "registry": registry,
            }
        )

    if not update_params:
        click.echo("No updates to apply.")
        return

    try:
        results = apply_updates(update_params)
        for r in results:
            if r["success"]:
                click.echo(
                    f"  ✓ Updated {r['collection']}: {len(r.get('installed', []))} recipes"
                )
            else:
                click.echo(
                    f"  ✗ Failed to update {r['collection']}: {r.get('error', 'unknown')}",
                    err=True,
                )
    except Exception as exc:
        click.echo(f"Error applying updates: {exc}", err=True)
        sys.exit(1)


@recipes.command("auto-update")
@click.option("--enable", is_flag=True, help="Enable auto-update")
@click.option("--disable", is_flag=True, help="Disable auto-update")
@click.option(
    "--schedule", "schedule", default=None, help='Cron schedule (default: "0 2 * * *")'
)
@click.option("--dry-run", is_flag=True, help="Run once without scheduling")
def recipes_auto_update(enable, disable, schedule, dry_run):
    """Configure or run OCI recipe auto-update."""
    from spark_pulse.config import config
    from spark_pulse.tools.oci_registry import run_auto_update

    if enable:
        config.oci_auto_update_enabled = True
        click.echo("Auto-update enabled.")
    elif disable:
        config.oci_auto_update_enabled = False
        click.echo("Auto-update disabled.")
    else:
        if schedule:
            config.oci_auto_update_schedule = schedule
            click.echo(f"Schedule set to: {schedule}")
        result = run_auto_update()
        if result.get("skipped"):
            click.echo(f"Auto-update skipped: {result.get('reason')}")
            return
        log_lines = result.get("log", [])
        for line in log_lines:
            click.echo(line)
        updated = result.get("updated", 0)
        if updated:
            click.echo(f"\n{updated} recipe(s) updated.")
        else:
            click.echo("\nNo recipes updated.")


@main.group()
def oci():
    """OCI registry management commands."""
    pass


@oci.command("add-registry")
@click.argument("name")
@click.argument("url")
@click.option("--default", "set_default", is_flag=True, help="Set as default registry")
def oci_add_registry(name, url, set_default):
    """Add a new OCI registry."""
    from spark_pulse.tools.oci_registry import add_registry

    try:
        reg = add_registry(
            {
                "name": name,
                "url": url,
                "enabled": True,
                "default": set_default,
                "auth": {},
            }
        )
        click.echo(f"Added registry '{reg['name']}' ({reg['url']}).")
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@oci.command("list-registries")
def oci_list_registries():
    """List configured OCI registries."""
    from spark_pulse.tools.oci_registry import list_registries

    regs = list_registries()
    if not regs:
        click.echo("No registries configured.")
        return

    click.echo(
        f"{'Name':<20} {'URL':<50} {'Enabled':<8} {'Default':<8} {'Connected':<8}"
    )
    click.echo("─" * 94)
    for r in regs:
        enabled = "yes" if r.get("enabled") else "no"
        default = "yes" if r.get("default") else "no"
        connected = "yes" if r.get("connected") else "no"
        click.echo(
            f"{r['name']:<20} {r.get('url', ''):<50} {enabled:<8} {default:<8} {connected:<8}"
        )


@oci.command("remove-registry")
@click.argument("name")
def oci_remove_registry(name):
    """Remove an OCI registry."""
    from spark_pulse.tools.oci_registry import remove_registry

    if remove_registry(name):
        click.echo(f"Removed registry '{name}'.")
    else:
        click.echo(f"Registry '{name}' not found.", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
