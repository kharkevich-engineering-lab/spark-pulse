"""Systemd service management for spark-pulse."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from spark_pulse.config import config

SERVICE_NAME = "spark-pulse.service"
SERVICE_TEMPLATE = """\
[Unit]
Description=Spark Pulse Web UI
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User={user}
WorkingDirectory={work_dir}
EnvironmentFile={env_file}
ExecStart={venv_bin}/uvicorn spark_pulse.app:app --host {host} --port {port} --workers 1
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

ENV_TEMPLATE = """\
SPARK_VLLM_PATH={spark_vllm_path}
WEBUI_PORT={port}
"""

_SYSTEMD_DIR = Path("/etc/systemd/system")


def _get_package_dir() -> Path:
    """Get the spark_pulse package directory."""
    return Path(__file__).resolve().parent


def _get_service_dir(user: str | None = None) -> Path:
    """Get the installation directory for the service files."""
    work_dir = Path("/opt/spark-pulse")
    if user:
        work_dir = Path.home() / "spark-pulse"
    return work_dir


def install_systemd(host: str = "0.0.0.0", port: int = 8100,
                    user: str | None = None, start: bool = True) -> None:
    """Install the systemd service unit file and environment file."""
    if user is None:
        user = os.environ.get("USER", "spark")

    package_dir = _get_package_dir()
    work_dir = package_dir.parent  # spark-pulse repo root
    data_dir = package_dir / "data"
    venv_bin = package_dir.parent / ".venv" / "bin"
    env_file = Path(f"/etc/spark-pulse.env")

    # Create env file
    env_content = ENV_TEMPLATE.format(
        spark_vllm_path=config.spark_vllm_path,
        port=port,
    )
    env_file.write_text(env_content)
    env_file.chmod(0o644)

    # Create service unit
    service_content = SERVICE_TEMPLATE.format(
        user=user,
        work_dir=str(work_dir),
        venv_bin=str(venv_bin),
        host=host,
        port=port,
        env_file=str(env_file),
        data_dir=str(data_dir),
    )
    service_file = _SYSTEMD_DIR / SERVICE_NAME
    service_file.write_text(service_content)
    service_file.chmod(0o644)

    # Reload systemd and enable
    _run_cmd(["systemctl", "daemon-reload"])
    _run_cmd(["systemctl", "enable", SERVICE_NAME])

    if start:
        _run_cmd(["systemctl", "start", SERVICE_NAME])


def uninstall_systemd() -> None:
    """Remove the systemd service unit and env file."""
    service_file = _SYSTEMD_DIR / SERVICE_NAME
    env_file = Path("/etc/spark-pulse.env")

    _run_cmd(["systemctl", "stop", SERVICE_NAME], check=False)
    _run_cmd(["systemctl", "disable", SERVICE_NAME], check=False)
    _run_cmd(["systemctl", "daemon-reload"], check=False)

    for f in (service_file, env_file):
        if f.exists():
            f.unlink()


def start_server() -> None:
    """Start the systemd service."""
    _run_cmd(["systemctl", "start", SERVICE_NAME])


def stop_server() -> None:
    """Stop the systemd service."""
    _run_cmd(["systemctl", "stop", SERVICE_NAME])


def get_status() -> str:
    """Get the systemd service status."""
    try:
        result = _run_cmd(["systemctl", "is-active", SERVICE_NAME], check=False)
        return result.stdout.strip()
    except FileNotFoundError:
        return "not-installed"
    except subprocess.CalledProcessError:
        return "not-found"


def _run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(
        cmd, capture_output=True, text=True, check=check
    )
