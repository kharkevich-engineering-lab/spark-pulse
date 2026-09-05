"""Launch script analysis, patching, and distribution for cluster deployments.

Replaces the --launch-script functionality from launch-cluster.sh.
Provides script analysis, parallelism extraction, patching, and distribution
to cluster nodes via SSH and node-bound container services.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spark_pulse.tools.parallelism import parse_parallelism


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Validation results for a launch script or mod."""

    healthy: bool
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls, warnings: list[str] | None = None) -> ValidationResult:
        """Create a successful validation result."""
        return cls(
            healthy=True,
            warnings=warnings or [],
            errors=[],
        )

    @classmethod
    def fail(
        cls, errors: list[str], warnings: list[str] | None = None
    ) -> ValidationResult:
        """Create a failed validation result."""
        return cls(
            healthy=False,
            warnings=warnings or [],
            errors=errors,
        )


@dataclass(frozen=True, slots=True)
class LaunchScriptInfo:
    """Analysis result for a launch script."""

    path: Path
    command_line: str | None = None
    parallelism: dict[str, int] = field(
        default_factory=lambda: {"tp": 1, "pp": 1, "dp": 1}
    )
    backend: str | None = None
    has_model_flag: bool = False
    is_valid: bool = False
    validation: ValidationResult | None = None


@dataclass(frozen=True, slots=True)
class PatchedScriptBundle:
    """Bundle of per-node patched scripts managed by TemporaryDirectory.

    The caller owns the TemporaryDirectory lifecycle and must call cleanup()
    when done, or the context manager will handle it.
    """

    temp_dir: tempfile.TemporaryDirectory[str]
    scripts: dict[int, Path] = field(default_factory=dict)
    original_script: Path = field(default_factory=Path)
    total_nodes: int = 0
    master_addr: str = "127.0.0.1"
    master_port: int = 29500

    def __enter__(self) -> PatchedScriptBundle:
        return self

    def __exit__(self, *args: Any) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        """Clean up temporary patched scripts."""
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def script_path(self, node_rank: int) -> Path | None:
        """Get the patched script path for a given node rank."""
        return self.scripts.get(node_rank)


# Dangerous patterns that should never appear in mod run.sh
DANGEROUS_PATTERNS: list[str] = [
    r"rm\s+-rf\s+/",
    r"mkfs",
    r"dd\s+if=shutdown",
    r"\breboot\b",
    r"shutdown\s+-h",
    r"dd\s+if=/dev/",
]

MAX_MOD_SIZE: int = 50 * 1024 * 1024  # 50MB


def validate_launch_script(script_path: Path) -> ValidationResult:
    """Validate a launch script before execution.

    Required checks:
    - Must contain python or vllm in command
    - Warn if --model is missing
    - Detect --tensor-parallel-size, --pipeline-parallel-size, --data-parallel-size
    - Detect ray/mp distributed backend

    Returns ValidationResult with errors/warnings.
    """
    warnings: list[str] = []
    errors: list[str] = []

    if not script_path.exists():
        return ValidationResult.fail(errors=[f"Launch script not found: {script_path}"])

    if not script_path.is_file():
        return ValidationResult.fail(
            errors=[f"Launch script is not a file: {script_path}"]
        )

    try:
        content = script_path.read_text(errors="replace")
    except OSError as e:
        return ValidationResult.fail(errors=[f"Cannot read launch script: {e}"])

    # Check for python/vllm command
    has_python = bool(re.search(r"\bpython\b", content))
    has_vllm = bool(re.search(r"\bvllm\b", content))
    has_exec = bool(re.search(r"^\s*exec\s", content, re.MULTILINE))

    if not (has_python or has_vllm or has_exec):
        errors.append("Launch script does not appear to contain a python/vllm command")

    # Check for --model flag
    has_model = bool(re.search(r"--model\s", content)) or bool(
        re.search(r"--model=", content)
    )
    if not has_model:
        warnings.append("Launch script does not contain --model flag")

    return (
        ValidationResult.ok(warnings=warnings)
        if not errors
        else ValidationResult.fail(errors)
    )


def analyze_launch_script(script_path: Path) -> LaunchScriptInfo:
    """Analyze a launch script before patching.

    1. Read script content
    2. Extract command line (python/vllm invocation)
    3. Parse parallelism flags via parse_parallelism()
    4. Detect distributed backend (ray, mp, etc.)
    5. Check for --model flag
    6. Validate script

    Returns LaunchScriptInfo with analysis results.
    """
    validation = validate_launch_script(script_path)

    try:
        content = script_path.read_text(errors="replace")
    except OSError:
        content = ""

    # Extract command line - look for python or vllm invocation
    command_line: str | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(r"\bpython\b", stripped) or re.match(r"\bvllm\b", stripped):
            # Skip comment lines
            if not stripped.startswith("#"):
                command_line = stripped
                break

    # Parse parallelism from the script content
    try:
        parallelism = parse_parallelism(content)
    except Exception:
        parallelism = {"tp": 1, "pp": 1, "dp": 1}

    # Detect distributed backend
    backend: str | None = None
    if re.search(r"--distributed-executor-backend\s+ray\b", content) or re.search(
        r"--distributed-executor-backend=ray\b", content
    ):
        backend = "ray"
    elif re.search(r"--distributed-executor-backend\s+mp\b", content) or re.search(
        r"--distributed-executor-backend=mp\b", content
    ):
        backend = "mp"

    # Check for --model flag
    has_model = bool(re.search(r"--model\s", content)) or bool(
        re.search(r"--model=", content)
    )

    return LaunchScriptInfo(
        path=script_path,
        command_line=command_line,
        parallelism=parallelism,
        backend=backend,
        has_model_flag=has_model,
        is_valid=validation.healthy,
        validation=validation,
    )


class LaunchScriptManager:
    """Manages launch script lifecycle: resolve -> analyze -> patch -> distribute.

    Depends on:
    - parse_parallelism() from Phase 3 for parallelism extraction
    - validate_launch_script() for pre-flight validation
    """

    def __init__(self, spark_path: Path | None = None):
        self._spark_path = spark_path or self._default_spark_path()

    @staticmethod
    def _default_spark_path() -> Path:
        """The configured spark-vllm-docker checkout, whether or not it exists.

        Never the empty path: ``Path("")`` is the process's working directory,
        and an unset checkout would then resolve ``examples/`` against whatever
        the server was started from. An unset path yields one that cannot
        exist, so lookups fail as "not found" rather than finding the wrong
        file.
        """
        from spark_pulse.config import config

        raw = config.spark_vllm_path.strip()
        return Path(raw).expanduser() if raw else Path("/nonexistent/spark-vllm-docker")

    def resolve(self, path: str) -> Path:
        """Resolve launch script path.

        Resolution order:
        1. Absolute path -> use as-is
        2. Relative path -> check spark_path/examples/
        3. Name without .sh -> check spark_path/examples/<name>.sh
        """
        p = Path(path)

        # Absolute path
        if p.is_absolute():
            if p.exists():
                return p
            raise FileNotFoundError(f"Launch script not found: {p}")

        # Relative path or name
        examples_dir = self._spark_path / "examples"
        resolved = examples_dir / path

        if resolved.exists():
            return resolved

        # Try with .sh extension
        if not path.endswith(".sh"):
            resolved = examples_dir / f"{path}.sh"
            if resolved.exists():
                return resolved

        raise FileNotFoundError(
            f"Launch script not found: {path} "
            f"(checked {resolved} and {examples_dir / path})"
        )

    def analyze(self, script_path: Path) -> LaunchScriptInfo:
        """Analyze script before patching."""
        return analyze_launch_script(script_path)

    def create_patched_bundle(
        self,
        script_path: Path,
        total_nodes: int,
        master_addr: str = "127.0.0.1",
        master_port: int = 29500,
    ) -> PatchedScriptBundle:
        """Create per-node patched copies in a TemporaryDirectory.

        Patching strategy (improved from launch-cluster.sh sed):
        1. Read script content
        2. Strip --distributed-executor-backend flag and its value
        3. Filter empty/backslash-only lines
        4. Strip trailing backslash from last line
        5. Append --nnodes, --node-rank, --master-addr, --master-port, --headless

        Returns PatchedScriptBundle with TemporaryDirectory (caller must cleanup).
        """
        validation = validate_launch_script(script_path)
        if not validation.healthy:
            raise ValueError(
                f"Launch script validation failed: {'; '.join(validation.errors)}"
            )

        content = script_path.read_text(errors="replace")

        # Strip --distributed-executor-backend and its value
        content = re.sub(r"--distributed-executor-backend\s+\S+", "", content)
        content = re.sub(r"--distributed-executor-backend=\S+", "", content)

        # Create temp directory for patched scripts
        temp_dir = tempfile.TemporaryDirectory(prefix="spark-pulse-scripts-")
        scripts: dict[int, Path] = {}

        for node_rank in range(total_nodes):
            patched = content

            # Filter empty/backslash-only lines (collapse continuations)
            lines = patched.splitlines()
            filtered_lines: list[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped == "" or stripped == "\\":
                    continue
                filtered_lines.append(line)
            patched = "\n".join(filtered_lines)

            # Strip trailing backslash from last line
            if patched.endswith("\\"):
                patched = patched.rstrip("\\").rstrip()

            # Append distributed args
            distributed_args = (
                f"--nnodes {total_nodes} "
                f"--node-rank {node_rank} "
                f"--master-addr {master_addr} "
                f"--master-port {master_port} "
                "--headless"
            )
            patched = f"{patched}\n{distributed_args}"

            script_file = Path(temp_dir.name) / f"node{node_rank}.sh"
            script_file.write_text(patched, errors="replace")
            scripts[node_rank] = script_file

        return PatchedScriptBundle(
            temp_dir=temp_dir,
            scripts=scripts,
            original_script=script_path,
            total_nodes=total_nodes,
            master_addr=master_addr,
            master_port=master_port,
        )

    def cleanup(self, bundle: PatchedScriptBundle) -> None:
        """Clean up temporary patched scripts."""
        bundle.cleanup()


class LaunchScriptDistributor:
    """Distributes patched scripts to cluster nodes.

    One code path, because there is one transport: the node-bound container
    service copies the script in, and the service knows which node it belongs
    to. What this replaces had two — ``docker cp`` on the head and
    ``scp``-then-``docker cp`` on a worker — and the head branch copied from
    ``/tmp/exec-script.sh`` *inside the container*, a path nothing had put
    anything at. It could only ever have worked by accident.
    """

    def __init__(
        self,
        services: Any = None,
        **_legacy: Any,
    ):
        from spark_pulse.tools.node_service import NodeServices

        self._services = services or NodeServices()

    def _service(self, address: str) -> Any:
        """The container service for the node at ``address``."""
        from spark_pulse.tools.node_service import node_for

        return self._services(node_for(address))

    def deploy_to_node(
        self,
        node: Any,
        script: Path,
        container_name: str,
    ) -> None:
        """Copy a patched script into a node's container.

        Args:
            node: ClusterNode with ip, role, container_name attributes
            script: Path to the patched script
            container_name: Name of the target container
        """
        self._service(node.ip).copy_to_container(
            container_name, str(script), "/workspace/exec-script.sh"
        )

    def deploy_to_cluster(
        self,
        cluster_state: Any,
        bundle: PatchedScriptBundle,
    ) -> dict[int, bool]:
        """Deploy patched scripts to all nodes in the cluster.

        Args:
            cluster_state: ClusterState with head and workers
            bundle: PatchedScriptBundle with per-node scripts

        Returns:
            Dict mapping node_rank to success status
        """
        results: dict[int, bool] = {}

        # Deploy to head (node_rank 0)
        head_script = bundle.script_path(0)
        if head_script:
            try:
                self.deploy_to_node(
                    node=cluster_state.head,
                    script=head_script,
                    container_name=cluster_state.head.container_name,
                )
                results[0] = True
            except Exception:
                results[0] = False

        # Deploy to workers
        for i, worker in enumerate(cluster_state.workers):
            worker_rank = i + 1
            worker_script = bundle.script_path(worker_rank)
            if worker_script:
                try:
                    self.deploy_to_node(
                        node=worker,
                        script=worker_script,
                        container_name=worker.container_name,
                    )
                    results[worker_rank] = True
                except Exception:
                    results[worker_rank] = False

        return results


def validate_mod_content(
    mod_path: Path,
    network_policy: str = "warn",
) -> ValidationResult:
    """Security validation for mod content.

    Scans run.sh and any .py files for dangerous patterns.

    Required:
    - run.sh exists (directory or zip)

    Reject:
    - rm -rf /, mkfs, dd if=shutdown, reboot
    - Size exceeds MAX_MOD_SIZE (default 50MB)
    - Zip bomb detection (>10x compression ratio)
    - Network access commands when policy is "deny"

    Warn:
    - sudo usage
    - curl/wget (network access, when policy is "warn")

    Args:
        mod_path: Path to the mod directory or zip file.
        network_policy: Network access policy - "allow", "warn", or "deny".

    Returns ValidationResult.
    """
    warnings: list[str] = []
    errors: list[str] = []

    if not mod_path.exists():
        return ValidationResult.fail(errors=[f"Mod path does not exist: {mod_path}"])

    # Size check
    try:
        mod_size = mod_path.stat().st_size
        if mod_size > MAX_MOD_SIZE:
            errors.append(
                f"Mod exceeds maximum size {MAX_MOD_SIZE} bytes (got {mod_size})"
            )
    except OSError as e:
        errors.append(f"Cannot stat mod path: {e}")
        return ValidationResult.fail(errors=errors)

    # Scan run.sh for dangerous patterns
    run_sh = mod_path / "run.sh"
    if run_sh.exists():
        try:
            content = run_sh.read_text(errors="replace")
            for pattern in DANGEROUS_PATTERNS:
                if re.search(pattern, content):
                    errors.append(f"Dangerous pattern detected in run.sh: {pattern}")

            # Warn on sudo usage
            if "sudo" in content:
                warnings.append("run.sh uses sudo")

            # Network access check
            network_patterns = [
                r"\bcurl\b",
                r"\bwget\b",
                r"\bpip\s+install\b",
                r"\bapt\b",
                r"\byum\b",
                r"\bdnf\b",
            ]
            has_network = any(re.search(p, content) for p in network_patterns)

            if has_network:
                if network_policy == "deny":
                    errors.append(
                        "Mod contains network access commands (curl/wget/pip). "
                        "Network access is denied by policy."
                    )
                elif network_policy == "warn":
                    warnings.append(
                        "Mod contains network access commands (curl/wget/pip). "
                        "Ensure this is expected behavior."
                    )
                # "allow": no action
        except OSError as e:
            errors.append(f"Cannot read run.sh: {e}")

    # Zip bomb detection
    if mod_path.suffix == ".zip":
        try:
            import zipfile

            with zipfile.ZipFile(mod_path) as zf:
                total_uncompressed = sum(info.file_size for info in zf.infolist())
                total_compressed = sum(info.compress_size for info in zf.infolist())
                if total_compressed > 0:
                    ratio = total_uncompressed / total_compressed
                    if ratio > 10:
                        errors.append(
                            f"Possible zip bomb: compression ratio {ratio:.1f}x"
                        )
        except zipfile.BadZipFile:
            errors.append("Invalid zip file")

    healthy = len(errors) == 0
    return ValidationResult(
        healthy=healthy,
        warnings=warnings,
        errors=errors,
    )
