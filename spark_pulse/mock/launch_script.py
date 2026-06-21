"""Mock launch script tools — scenario-driven simulation.

Returns deterministic results without accessing the filesystem.
Mirrors the real launch_script.py API exactly.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spark_pulse.tools.launch_script import (
    LaunchScriptInfo,
    PatchedScriptBundle,
    ValidationResult,
)


@dataclass
class _MockScriptBundle:
    """Internal bundle for mock script patching."""

    temp_dir: tempfile.TemporaryDirectory[str]
    scripts: dict[int, Path]
    original_script: Path
    total_nodes: int
    master_addr: str
    master_port: int

    def cleanup(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass


# Simulated launch scripts for different scenarios
_MOCK_SCRIPTS: dict[str, str] = {
    "qwen.sh": (
        "#!/bin/bash\n"
        "python -m vllm.entrypoints.openai.api_server "
        "--model Qwen/Qwen2.5-7B-Instruct "
        "--tensor-parallel-size 4 "
        "--distributed-executor-backend ray\n"
    ),
    "llama.sh": (
        "#!/bin/bash\n"
        "python -m vllm.entrypoints.openai.api_server "
        "--model meta-llama/Llama-3.1-8B "
        "--pipeline-parallel-size 2\n"
    ),
    "dangerous.sh": (
        "#!/bin/bash\n"
        "rm -rf / && echo 'destroyed'\n"
    ),
}


class MockLaunchScriptManager:
    """Mock launch script manager for simulation mode.

    Scenario-driven simulation:
    - "valid": returns valid script analysis
    - "missing_model": warns about missing --model flag
    - "dangerous": detects dangerous patterns
    """

    def __init__(self, scenario: str = "valid"):
        self._scenario = scenario
        self._bundles: dict[str, _MockScriptBundle] = {}

    def resolve(self, path: str) -> Path:
        """Resolve a mock launch script path."""
        if path in _MOCK_SCRIPTS:
            return Path(f"/mock/examples/{path}")
        raise FileNotFoundError(f"Launch script not found: {path}")

    def analyze(self, script_path: Path) -> LaunchScriptInfo:
        """Analyze a mock launch script."""
        script_name = script_path.name

        # Determine parallelism based on scenario
        parallelism: dict[str, int] = {"tp": 1, "pp": 1, "dp": 1}
        backend: str | None = None
        has_model: bool = True
        command_line: str | None = None
        warnings: list[str] = []
        errors: list[str] = []

        if script_name == "qwen.sh" or self._scenario == "valid":
            parallelism = {"tp": 4, "pp": 1, "dp": 1}
            backend = "ray"
            command_line = (
                "python -m vllm.entrypoints.openai.api_server "
                "--model Qwen/Qwen2.5-7B-Instruct "
                "--tensor-parallel-size 4 "
                "--distributed-executor-backend ray"
            )
        elif script_name == "llama.sh" or self._scenario == "missing_model":
            parallelism = {"tp": 1, "pp": 2, "dp": 1}
            backend = None
            has_model = False
            warnings.append("Launch script does not contain --model flag")
            command_line = (
                "python -m vllm.entrypoints.openai.api_server "
                "--pipeline-parallel-size 2"
            )
        elif script_name == "dangerous.sh":
            errors.append(
                "Launch script does not appear to contain a python/vllm command"
            )

        validation = ValidationResult(
            healthy=len(errors) == 0,
            warnings=warnings,
            errors=errors,
        )

        return LaunchScriptInfo(
            path=script_path,
            command_line=command_line,
            parallelism=parallelism,
            backend=backend,
            has_model_flag=has_model,
            is_valid=len(errors) == 0,
            validation=validation,
        )

    def create_patched_bundle(
        self,
        script_path: Path,
        total_nodes: int,
        master_addr: str = "127.0.0.1",
        master_port: int = 29500,
    ) -> PatchedScriptBundle:
        """Create mock patched script bundle."""
        script_name = script_path.name
        original_content = _MOCK_SCRIPTS.get(script_name, "#!/bin/bash\necho 'mock'\n")

        # Validate
        validation = ValidationResult(healthy=True)
        if script_name == "dangerous.sh":
            validation = ValidationResult.fail(
                errors=["Launch script does not appear to contain a python/vllm command"]
            )
            raise ValueError(
                "Launch script validation failed: "
                "Launch script does not appear to contain a python/vllm command"
            )

        temp_dir = tempfile.TemporaryDirectory(
            prefix="spark-pulse-mock-scripts-"
        )
        scripts: dict[int, Path] = {}

        for node_rank in range(total_nodes):
            patched = original_content
            # Strip --distributed-executor-backend
            import re
            patched = re.sub(
                r'--distributed-executor-backend\s+\S+', '', patched
            )
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

        bundle = _MockScriptBundle(
            temp_dir=temp_dir,
            scripts=scripts,
            original_script=script_path,
            total_nodes=total_nodes,
            master_addr=master_addr,
            master_port=master_port,
        )
        key = f"{script_path}-{total_nodes}"
        self._bundles[key] = bundle

        return PatchedScriptBundle(
            temp_dir=temp_dir,
            scripts=scripts,
            original_script=script_path,
            total_nodes=total_nodes,
            master_addr=master_addr,
            master_port=master_port,
        )

    def cleanup(self, bundle: PatchedScriptBundle) -> None:
        """Clean up mock patched scripts."""
        bundle.cleanup()


class MockLaunchScriptDistributor:
    """Mock launch script distributor for simulation mode."""

    def __init__(self):
        self._deployments: list[dict[str, Any]] = []

    def deploy_to_node(
        self,
        node: Any,
        script: Path,
        container_name: str,
    ) -> None:
        """Mock deployment to a node."""
        self._deployments.append({
            "node_ip": node.ip,
            "node_role": node.role,
            "container": container_name,
            "script": str(script),
            "timestamp": "2026-06-20T00:00:00Z",
        })

    def deploy_to_cluster(
        self,
        cluster_state: Any,
        bundle: PatchedScriptBundle,
    ) -> dict[int, bool]:
        """Mock deployment to all cluster nodes."""
        results: dict[int, bool] = {}

        # Head node
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

        # Workers
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
