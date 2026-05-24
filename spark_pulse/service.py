"""Systemd service management for spark-pulse."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

from spark_pulse.config import config

SERVICE_NAME = "spark-pulse.service"
ServiceScope = Literal["system", "user"]

_SYSTEM_SERVICE_TEMPLATE = """\
[Unit]
Description=Spark Pulse Web UI
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User={user}
WorkingDirectory={work_dir}
EnvironmentFile={env_file}
ExecStart={python_exe} -m uvicorn spark_pulse.app:app --host {host} --port {port} --workers 1
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
ProtectSystem=full
ReadWritePaths={data_dir}
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""

_USER_SERVICE_TEMPLATE = """\
[Unit]
Description=Spark Pulse Web UI
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory={work_dir}
EnvironmentFile={env_file}
ExecStart={python_exe} -m uvicorn spark_pulse.app:app --host {host} --port {port} --workers 1
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
ProtectSystem=full
ReadWritePaths={data_dir}
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""

ENV_TEMPLATE = """\
SPARK_VLLM_PATH={spark_vllm_path}
WEBUI_PORT={port}
"""

_SYSTEMD_DIR = Path("/etc/systemd/system")


def _get_package_dir() -> Path:
    """Get the spark_pulse package directory."""
    return Path(__file__).resolve().parent


def _get_service_dir(scope: ServiceScope = "system") -> Path:
    """Get the service working directory by scope."""
    if scope == "user":
        return Path.home() / ".local" / "share" / "spark-pulse"
    return Path("/var/lib/spark-pulse")


def _get_env_file(scope: ServiceScope = "system") -> Path:
    """Get the environment file path by scope."""
    if scope == "user":
        return Path.home() / ".config" / "spark-pulse" / "spark-pulse.env"
    return Path("/etc/spark-pulse.env")


def _get_unit_dir(scope: ServiceScope = "system") -> Path:
    """Get the systemd unit directory by scope."""
    if scope == "user":
        return Path.home() / ".config" / "systemd" / "user"
    return _SYSTEMD_DIR


def _systemctl_cmd(args: list[str], scope: ServiceScope = "system") -> list[str]:
    """Build a systemctl command for the selected scope."""
    cmd = ["systemctl"]
    if scope == "user":
        cmd.append("--user")
    cmd.extend(args)
    return cmd


def _render_service_content(
    *,
    scope: ServiceScope,
    user: str,
    work_dir: Path,
    env_file: Path,
    host: str,
    port: int,
    data_dir: Path,
) -> str:
    """Render systemd unit content for system or user scope."""
    template = _USER_SERVICE_TEMPLATE if scope == "user" else _SYSTEM_SERVICE_TEMPLATE
    return template.format(
        user=user,
        work_dir=str(work_dir),
        python_exe=sys.executable,
        host=host,
        port=port,
        env_file=str(env_file),
        data_dir=str(data_dir),
    )


def install_systemd(
    host: str = "0.0.0.0",
    port: int = 8100,
    user: str | None = None,
    start: bool = True,
    scope: ServiceScope = "system",
) -> None:
    """Install the systemd service unit file and environment file."""
    if user is None:
        user = os.environ.get("USER", "spark")

    package_dir = _get_package_dir()
    work_dir = _get_service_dir(scope)
    data_dir = package_dir / "data"
    env_file = _get_env_file(scope)
    unit_dir = _get_unit_dir(scope)

    if scope == "user":
        unit_dir.mkdir(parents=True, exist_ok=True)
        env_file.parent.mkdir(parents=True, exist_ok=True)

    work_dir.mkdir(parents=True, exist_ok=True)

    # Create env file
    env_content = ENV_TEMPLATE.format(
        spark_vllm_path=config.spark_vllm_path,
        port=port,
    )
    env_file.write_text(env_content)
    if scope == "system":
        env_file.chmod(0o644)

    # Create service unit
    service_content = _render_service_content(
        scope=scope,
        user=user,
        work_dir=work_dir,
        env_file=env_file,
        host=host,
        port=port,
        data_dir=data_dir,
    )
    service_file = unit_dir / SERVICE_NAME
    service_file.write_text(service_content)
    if scope == "system":
        service_file.chmod(0o644)

    # Reload systemd and enable
    _run_cmd(_systemctl_cmd(["daemon-reload"], scope=scope))
    _run_cmd(_systemctl_cmd(["enable", SERVICE_NAME], scope=scope))

    if start:
        _run_cmd(_systemctl_cmd(["start", SERVICE_NAME], scope=scope))


def uninstall_systemd(scope: ServiceScope = "system") -> None:
    """Remove the systemd service unit and env file."""
    service_file = _get_unit_dir(scope) / SERVICE_NAME
    env_file = _get_env_file(scope)

    _run_cmd(_systemctl_cmd(["stop", SERVICE_NAME], scope=scope), check=False)
    _run_cmd(_systemctl_cmd(["disable", SERVICE_NAME], scope=scope), check=False)
    _run_cmd(_systemctl_cmd(["daemon-reload"], scope=scope), check=False)

    for f in (service_file, env_file):
        if f.exists():
            f.unlink()


def start_server(scope: ServiceScope = "system") -> None:
    """Start the systemd service."""
    _run_cmd(_systemctl_cmd(["start", SERVICE_NAME], scope=scope))


def stop_server(scope: ServiceScope = "system") -> None:
    """Stop the systemd service."""
    _run_cmd(_systemctl_cmd(["stop", SERVICE_NAME], scope=scope))


def get_status(scope: ServiceScope = "system") -> str:
    """Get the systemd service status."""
    try:
        result = _run_cmd(
            _systemctl_cmd(["is-active", SERVICE_NAME], scope=scope), check=False
        )
        return result.stdout.strip()
    except FileNotFoundError:
        return "not-installed"
    except subprocess.CalledProcessError:
        return "not-found"


def _run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)
