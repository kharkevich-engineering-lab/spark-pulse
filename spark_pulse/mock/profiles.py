"""What kind of machine a simulated node is (#174).

Simulation had one machine: a DGX Spark — aarch64, one GB10 whose memory is
the host's (``[N/A]`` from ``nvidia-smi``), Docker. Everything added for a
workstation — discrete GPU memory (#171), recipe fit by memory (#173), the
x86_64 agent (#170), rootless Podman and its checks (#165–#168) — only ran on
the one desktop that has one, and CI never saw it.

So a simulated node has a profile, and the two there are describe real
machines, not convenient ones:

* :data:`SPARK` is the simulated node as it always was. It reports no agent
  facts, exactly as before — simulation has no agent — so nothing that reads a
  Spark in simulation changes.
* :data:`WORKSTATION` is the lab desktop the Podman spike (#165) ran on, with
  the numbers its agent reported: an RTX 3060 with 12 GiB of its own memory,
  24 CPUs, 94 GiB of host memory, rootless Podman 4.9.3 with a CDI spec, and
  ``linux/amd64``. It reports facts, the way a connected agent does, so the
  paths keyed on them (the runtime checks, the platform a recipe needs) run.

Selected per node by address with :func:`use`, or for every node with
``SPARK_PULSE_SIM_PROFILE=workstation`` (read on each ask, so a simulation
backend started with it is a workstation fleet).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "NodeProfile",
    "PROFILES",
    "SPARK",
    "WORKSTATION",
    "profile_for",
    "reset",
    "use",
]

GiB = 1024**3
MiB = 1024**2


@dataclass(frozen=True)
class NodeProfile:
    """One kind of machine: what its probes, stats and facts answer."""

    name: str
    #: Whether the node reports agent facts. A Spark in simulation never did.
    reports_facts: bool
    platform: str
    cpu_count: int
    memory_total_bytes: int
    memory_available_bytes: int
    gpu_name: str
    gpu_uuid: str
    #: ``None`` when the GPU's memory is the host's and ``nvidia-smi`` says
    #: ``[N/A]`` — a GB10. A number is a card with memory of its own.
    gpu_memory_total_bytes: int | None
    gpu_memory_used_bytes: int | None
    gpu_power_limit_watts: float
    container_runtime: str
    runtime_version: str
    rootless: bool
    cdi_devices: tuple[str, ...] = ()
    #: What ``podman info`` would call the image store, for the disk check.
    store_root: str = ""
    extra_facts: dict[str, Any] = field(default_factory=dict)

    @property
    def discrete(self) -> bool:
        return self.gpu_memory_total_bytes is not None

    def facts(self) -> Any:
        """The ``NodeFacts`` this machine's agent would send."""
        from spark_pulse.mock import agent_node

        return agent_node.facts(
            cpu_count=self.cpu_count,
            memory_bytes=self.memory_total_bytes,
            gpu_count=1,
            container_runtime=self.container_runtime,
            runtime_version=self.runtime_version,
            rootless=self.rootless,
            cdi_devices=list(self.cdi_devices),
            platform=self.platform,
            **self.extra_facts,
        )

    def nvidia_smi_row(self) -> str:
        """``index, name, memory.total, memory.used`` (MiB), as the probe asks."""
        if self.gpu_memory_total_bytes is None:
            return f"0, {self.gpu_name}, [N/A], [N/A]"
        used = self.gpu_memory_used_bytes or 0
        return (
            f"0, {self.gpu_name}, {self.gpu_memory_total_bytes // MiB}, "
            f"{used // MiB}"
        )


SPARK = NodeProfile(
    name="spark",
    reports_facts=False,
    platform="linux/arm64",
    cpu_count=20,
    memory_total_bytes=130_000_000_000,
    memory_available_bytes=104_000_000_000,
    gpu_name="NVIDIA GB10",
    gpu_uuid="GPU-00000000-0000-0000-0000-000000000000",
    gpu_memory_total_bytes=None,
    gpu_memory_used_bytes=None,
    gpu_power_limit_watts=140.0,
    container_runtime="docker",
    runtime_version="",
    rootless=False,
)

#: The lab desktop, as its agent reported it on 2026-11-13.
WORKSTATION = NodeProfile(
    name="workstation",
    reports_facts=True,
    platform="linux/amd64",
    cpu_count=24,
    memory_total_bytes=98_495_524 * 1024,
    memory_available_bytes=75_578_688 * 1024,
    gpu_name="NVIDIA GeForce RTX 3060",
    gpu_uuid="GPU-c52a39b9-8357-e610-9835-2c7621ffb255",
    gpu_memory_total_bytes=12_288 * MiB,
    gpu_memory_used_bytes=960 * MiB,
    gpu_power_limit_watts=170.0,
    container_runtime="podman",
    runtime_version="4.9.3",
    rootless=True,
    cdi_devices=(
        "nvidia.com/gpu=0",
        "nvidia.com/gpu=GPU-c52a39b9-8357-e610-9835-2c7621ffb255",
        "nvidia.com/gpu=all",
    ),
    store_root="/home/ark/.local/share/containers/storage",
    # A Podman node runs no Docker daemon; the field is Docker's own version.
    extra_facts={"docker_version": ""},
)

PROFILES = {profile.name: profile for profile in (SPARK, WORKSTATION)}

#: Profiles chosen per node address, over the environment's default.
_chosen: dict[str, NodeProfile] = {}


def use(address: str, profile: NodeProfile) -> None:
    """Make the simulated node at ``address`` this kind of machine."""
    _chosen[address] = profile


def reset() -> None:
    _chosen.clear()


def profile_for(address: str) -> NodeProfile:
    """The profile of the simulated node at ``address``."""
    if address in _chosen:
        return _chosen[address]
    default = os.environ.get("SPARK_PULSE_SIM_PROFILE", "").strip().lower()
    return PROFILES.get(default, SPARK)
