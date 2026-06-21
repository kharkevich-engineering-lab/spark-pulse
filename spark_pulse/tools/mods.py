"""Tools for reading spark-vllm-docker mods.

Provides mod listing, inspection, cluster-wide deployment orchestration,
and security validation for mod content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from spark_pulse.config import config
from spark_pulse.tools.launch_script import (
    DANGEROUS_PATTERNS,
    MAX_MOD_SIZE,
    ValidationResult,
    validate_mod_content as validate_mod_content_raw,
)

_ASSET_EXTENSIONS = {
    ".patch",
    ".diff",
    ".jinja",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".sh",
}


@dataclass(frozen=True, slots=True)
class ModDeployment:
    """Mod deployment with tracking for cluster-wide orchestration."""

    mod_name: str
    mod_path: Path
    target: Literal["head", "workers", "all"]
    completed_nodes: list[str] = field(default_factory=list)
    failed_nodes: list[str] = field(default_factory=list)


class ModOrchestrator:
    """Orchestrates mod deployment across cluster nodes.

    Depends on:
    - SSHClient + RemoteDockerService from Phase 3
    - validate_mod_content() for security validation
    """

    def __init__(
        self,
        ssh_client: Any = None,
        remote_docker: Any = None,
    ):
        self._ssh = ssh_client
        self._docker = remote_docker

    def apply_mod_cluster(
        self,
        mod_deployment: ModDeployment,
        cluster_state: Any,
    ) -> ModDeployment:
        """Apply mod to correct nodes based on target.

        - target=head: apply to head container only
        - target=workers: apply to all worker containers
        - target=all: apply to head + all workers

        Returns ModDeployment with completed_nodes tracked.
        On failure, returns partial result for rollback.
        """
        completed: list[str] = []
        failed: list[str] = []

        # Determine target nodes
        target_nodes: list[Any] = []
        if mod_deployment.target in ("head", "all"):
            target_nodes.append(cluster_state.head)
        if mod_deployment.target in ("workers", "all"):
            target_nodes.extend(cluster_state.workers)

        for node in target_nodes:
            try:
                self._apply_mod_to_node(
                    mod_deployment.mod_path,
                    node,
                    mod_deployment.mod_name,
                )
                completed.append(node.ip)
            except Exception as e:
                failed.append(node.ip)

        return ModDeployment(
            mod_name=mod_deployment.mod_name,
            mod_path=mod_deployment.mod_path,
            target=mod_deployment.target,
            completed_nodes=completed,
            failed_nodes=failed,
        )

    def rollback_mod(
        self,
        mod_deployment: ModDeployment,
        cluster_state: Any,
    ) -> list[str]:
        """Rollback a mod on completed nodes.

        Executes cleanup on nodes where mod was successfully applied
        but the overall deployment failed.

        Returns list of nodes where rollback succeeded.
        """
        rolled_back: list[str] = []

        target_nodes: list[Any] = []
        if mod_deployment.target in ("head", "all"):
            target_nodes.append(cluster_state.head)
        if mod_deployment.target in ("workers", "all"):
            target_nodes.extend(cluster_state.workers)

        for node in target_nodes:
            if node.ip in mod_deployment.completed_nodes:
                try:
                    self._rollback_mod_from_node(
                        mod_deployment.mod_name,
                        node,
                    )
                    rolled_back.append(node.ip)
                except Exception:
                    pass  # Rollback failure is non-fatal

        return rolled_back

    def validate_mod(
        self,
        mod_path: Path,
    ) -> ValidationResult:
        """Validate mod before deployment.

        Delegates to validate_mod_content() for security checks.
        """
        return validate_mod_content(mod_path)

    def _apply_mod_to_node(
        self,
        mod_path: Path,
        node: Any,
        mod_name: str,
    ) -> None:
        """Apply a mod to a single node's container."""
        if self._docker is None:
            raise RuntimeError(
                "ModOrchestrator requires a RemoteDockerService"
            )

        container_name = node.container_name
        remote_path = f"/workspace/mods/{mod_name}"

        # Copy mod files to container
        self._docker.exec_container(
            host=node.ip,
            container=container_name,
            command=["mkdir", "-p", remote_path],
        )

        # Copy run.sh
        run_sh = mod_path / "run.sh"
        if run_sh.exists():
            self._docker.copy_to_container(
                host=node.ip,
                container=container_name,
                local_path=str(run_sh),
                remote_path=f"{remote_path}/run.sh",
            )

        # Copy other files
        for file_path in mod_path.iterdir():
            if file_path.is_file() and file_path.name != "run.sh":
                self._docker.copy_to_container(
                    host=node.ip,
                    container=container_name,
                    local_path=str(file_path),
                    remote_path=f"{remote_path}/{file_path.name}",
                )

        # Execute run.sh
        self._docker.exec_container(
            host=node.ip,
            container=container_name,
            command=["bash", f"{remote_path}/run.sh"],
        )

    def _rollback_mod_from_node(
        self,
        mod_name: str,
        node: Any,
    ) -> None:
        """Remove a mod from a single node's container."""
        if self._docker is None:
            raise RuntimeError(
                "ModOrchestrator requires a RemoteDockerService"
            )

        self._docker.exec_container(
            host=node.ip,
            container=node.container_name,
            command=["rm", "-rf", f"/workspace/mods/{mod_name}"],
        )


def validate_mod_content(mod_path: Path) -> ValidationResult:
    """Security validation for mod content.

    Delegates to the shared implementation in launch_script.py.
    """
    return validate_mod_content_raw(mod_path)


def _mods_dir() -> Path:
    return Path(config.spark_vllm_path) / "mods"


def _extract_description(run_sh: Path) -> str:
    """Extract description from leading comments or first echo statement in run.sh."""
    try:
        lines = run_sh.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    desc: list[str] = []
    for line in lines:
        if line.startswith("#!/"):
            continue
        if line.startswith("#"):
            text = line.lstrip("#").strip()
            if text:
                desc.append(text)
        elif line.strip() == "":
            if desc:
                break  # blank line ends the leading comment block
        else:
            break  # first real code line ends it

    if desc:
        return " ".join(desc)

    # Fallback: look for the first `echo "..."` line anywhere in the script
    for line in lines:
        m = re.match(r'\s*echo\s+["\'](.+?)["\']', line)
        if m:
            return m.group(1).strip("=:- ").rstrip(".")
    return ""


def _asset_kind(name: str) -> str:
    ext = Path(name).suffix
    if ext in (".patch", ".diff"):
        return "patch"
    if ext == ".jinja":
        return "template"
    if ext == ".py":
        return "python"
    if ext in (".yaml", ".yml"):
        return "yaml"
    if ext == ".sh":
        return "script"
    return "file"


def _mod_info(mod_dir: Path, include_script: bool = False) -> dict[str, Any]:
    run_sh = mod_dir / "run.sh"
    files = sorted(
        (
            {"name": f.name, "kind": _asset_kind(f.name)}
            for f in mod_dir.iterdir()
            if f.is_file() and f.name != "run.sh"
        ),
        key=lambda x: x["name"],
    )
    info: dict[str, Any] = {
        "id": mod_dir.name,
        "description": _extract_description(run_sh) if run_sh.exists() else "",
        "files": files,
        "has_patches": any(f["kind"] == "patch" for f in files),
    }
    if include_script and run_sh.exists():
        info["script"] = run_sh.read_text(errors="replace")
    return info


def list_mods() -> list[dict[str, Any]]:
    d = _mods_dir()
    if not d.exists():
        return []
    return [_mod_info(p) for p in sorted(d.iterdir()) if p.is_dir()]


def get_mod(mod_id: str) -> dict[str, Any] | None:
    # Sanitise: no path traversal
    if "/" in mod_id or ".." in mod_id:
        return None
    mod_dir = _mods_dir() / mod_id
    if not mod_dir.is_dir():
        return None
    return _mod_info(mod_dir, include_script=True)
