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


if __name__ == "__main__":
    main()
