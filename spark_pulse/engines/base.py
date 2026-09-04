"""Engine abstraction — spec model, launch script container and base class.

An *engine* is a serving framework (vLLM, SGLang). Everything framework
specific lives here or in the concrete engine module; the runtime and cluster
code stay engine agnostic.

The declarative half of an engine comes from an ``engine.yaml`` published by
the ``spark-pulse-engine`` repo (see ``spark-engine.schema.json``); it is parsed
into :class:`EngineSpec`. The Python subclass only holds rendering logic.
"""

from __future__ import annotations

import re
import shlex
import socket
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: The largest topology any engine may be asked for.
#:
#: NVIDIA's Sync cluster assistant supports "two to a maximum of four DGX Spark
#: devices" (https://docs.nvidia.com/sync/latest/cluster-assistant.html), and
#: its own playbooks cover exactly three arrangements: two nodes on one cable,
#: three in a switchless ring, and four behind a QSFP switch. A fifth node has
#: no NVIDIA-documented configuration and no way for us to check one, so the
#: plan refuses it by number rather than letting an operator discover it as a
#: rendezvous that never forms. Larger clusters do exist in the community —
#: an eight-node build behind two MikroTik CRS812-DDQ switches is described at
#: https://forums.developer.nvidia.com/t/6x-spark-setup/354399 — but nothing
#: authoritative describes how to configure one, so we do not pretend to.
MAX_CLUSTER_NODES = 4

# ── Spec model (mirrors engine.yaml) ─────────────────────────────────────────


class EnginePorts(BaseModel):
    model_config = ConfigDict(extra="allow")

    api: int = 8000
    rendezvous: int | None = None


class EngineContainer(BaseModel):
    """Resource profile for the container that runs this engine."""

    model_config = ConfigDict(extra="allow")

    privileged: bool = False
    ipc_host: bool = False
    network_host: bool = False
    shm_size_gb: int | None = None
    devices: list[str] = Field(default_factory=list)
    cap_add: list[str] = Field(default_factory=list)
    ulimits: dict[str, str] = Field(default_factory=dict)
    keepalive: str = "sleep infinity"


class EngineMultiNode(BaseModel):
    model_config = ConfigDict(extra="allow")

    style: str = "none"
    extra_args: list[str] = Field(default_factory=list)


class EngineRuntime(BaseModel):
    model_config = ConfigDict(extra="allow")

    python: str | None = None
    workdir: str | None = None
    site_packages: str | None = None
    serve: str = ""
    model_arg: str = "positional"
    readiness: str = "/health"
    models_endpoint: str | None = None
    metrics: str | None = None
    ports: EnginePorts = Field(default_factory=EnginePorts)
    cache_mounts: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    container: EngineContainer = Field(default_factory=EngineContainer)
    multi_node: EngineMultiNode = Field(default_factory=EngineMultiNode)
    param_flags: dict[str, str] = Field(default_factory=dict)


class EngineCapabilities(BaseModel):
    """What an engine image claims it can do.

    ``solo``, ``cluster`` and ``mesh`` are the three topology sizes, and
    :meth:`Engine.supports_size` is the one place that reads them:

    * ``solo`` — one node.
    * ``cluster`` — two nodes, the size NVIDIA publishes guidance for.
    * ``mesh`` — three or four nodes. NVIDIA gives two ways to get there: a
      QSFP switch, or — at three nodes only — a switchless ring cabling port 0
      of one Spark into port 1 of the next
      (https://build.nvidia.com/spark/nccl/three-sparks). Either way the
      arrangement is not more of a pair: the ring needs NCCL settings a pair
      does not, and daisy-chaining three Sparks is reported to sustain only
      100G between each pair rather than 200G (``spark-vllm-docker``
      ``docs/NETWORKING.md`` line 43 — NVIDIA publishes no ring bandwidth
      figure either way). Tensor parallelism is also documented as wanting a
      power-of-two node count (line 6). So an engine that has only ever been
      arranged as a pair must claim this rather than be assumed to generalise.

    None of these is a claim that the size has been *run*; it is a claim that
    the engine renders and is documented for it. See
    :data:`MAX_CLUSTER_NODES` for the ceiling no engine may claim past.
    """

    model_config = ConfigDict(extra="allow")

    mods: bool = False
    pr_mods: bool = False
    solo: bool = True
    cluster: bool = False
    mesh: bool = False


class EngineVerification(BaseModel):
    model_config = ConfigDict(extra="allow")

    nodes: int
    model: str
    date: str
    tp: int | None = None
    pp: int | None = None
    notes: str = ""


class EngineSpec(BaseModel):
    """One engine image definition, parsed from an ``engine.yaml``."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = "1"
    engine: str
    variant: str = "default"
    description: str = ""
    image: str = ""
    version: str = "0.0.0"
    framework_version: str = ""
    """Version of the serving framework *inside* the image (``0.28.1``).

    Distinct from :attr:`version`, which is the image build. A launch flag is
    only safe to render when the framework in the image understands it, so
    this is what :meth:`Engine.version_supported` checks.
    """
    tag: str | None = None
    digest: str | None = None
    legacy_tags: list[str] = Field(default_factory=list)
    # False when the index says this image was never published. Pulling such a
    # reference returns a 403 rather than an image, so it must not be offered.
    available: bool = True
    arch: list[str] = Field(default_factory=lambda: ["linux/arm64"])
    gpu_arch: list[str] = Field(default_factory=list)
    sources: dict[str, Any] = Field(default_factory=dict)
    build_args: dict[str, Any] = Field(default_factory=dict)
    runtime: EngineRuntime = Field(default_factory=EngineRuntime)
    capabilities: EngineCapabilities = Field(default_factory=EngineCapabilities)
    verified: list[EngineVerification] = Field(default_factory=list)
    source: str = "bundled"
    """Where this spec came from: ``bundled`` or an engine index reference."""

    @property
    def key(self) -> str:
        return f"{self.engine}/{self.variant}"

    @property
    def image_ref(self) -> str:
        """Fully qualified image reference (``repo:tag`` or ``repo@digest``)."""
        if not self.image:
            return ""
        if self.digest:
            return f"{self.image}@{self.digest}"
        tag = self.tag or self.version
        if "/" in tag:  # already a full reference
            return tag
        return f"{self.image}:{tag}"

    def summary(self) -> dict[str, Any]:
        """Compact dict for the REST layer."""
        return {
            "engine": self.engine,
            "variant": self.variant,
            "key": self.key,
            "description": self.description,
            "image": self.image,
            "image_ref": self.image_ref,
            "version": self.version,
            "tag": self.tag or self.version,
            "digest": self.digest,
            "legacy_tags": list(self.legacy_tags),
            "capabilities": self.capabilities.model_dump(),
            "verified": [v.model_dump() for v in self.verified],
            "ports": self.runtime.ports.model_dump(),
            "readiness": self.runtime.readiness,
            "models_endpoint": self.runtime.models_endpoint,
            "metrics": self.runtime.metrics,
            "source": self.source,
        }


# ── Topology and rendering result ────────────────────────────────────────────


@dataclass
class NodeInfo:
    """One node participating in a launch."""

    host: str
    ip: str = ""
    eth_if: str = ""
    ib_if: str = ""
    #: Whether this machine is cabled as the switchless mesh — all four CX7
    #: ports up, port 0 of one Spark into port 1 of the next. It carries three
    #: NCCL settings a single-cable fabric must not get; see
    #: :data:`~spark_pulse.tools.discovery.MESH_NCCL_ENV`.
    mesh: bool = False

    def address(self) -> str:
        return self.ip or self.host

    @classmethod
    def local(cls) -> NodeInfo:
        """This machine, as a node.

        The address is loopback rather than the fabric IP: a one-node launch
        talks to itself, and loopback stays valid when the fabric link is down
        or unaddressed. No interface names, because pinning one is only ever
        right above a single node (see :meth:`Engine.pinning_env`).
        """
        return cls(host=socket.gethostname() or "localhost", ip="127.0.0.1")


@dataclass
class Topology:
    """How many nodes take part and which one is the head.

    Total: there is always at least one node, and it is always a real one. An
    empty node list means "just this machine", so every size is rendered the
    same way and no caller has to ask whether a node exists.
    """

    nodes: list[NodeInfo] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.nodes:
            self.nodes = [NodeInfo.local()]

    @property
    def size(self) -> int:
        return len(self.nodes)

    @property
    def is_solo(self) -> bool:
        """One node. For display and record keeping — never a code path."""
        return self.size == 1

    @property
    def head(self) -> NodeInfo:
        return self.nodes[0]

    def node(self, rank: int) -> NodeInfo | None:
        if 0 <= rank < len(self.nodes):
            return self.nodes[rank]
        return None

    @classmethod
    def solo(cls, node: NodeInfo | None = None) -> Topology:
        """A one-node topology: *node*, or this machine."""
        return cls(nodes=[node or NodeInfo.local()])


@dataclass
class LaunchScript:
    """A rendered per-rank launch: the bash text plus the env it needs."""

    node_rank: int
    command: str
    env: dict[str, str] = field(default_factory=dict)
    host: str = ""
    script: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_rank": self.node_rank,
            "host": self.host,
            "command": self.command,
            "env": dict(self.env),
            "script": self.script,
        }


class EngineError(ValueError):
    """Raised when a launch cannot be rendered."""


_VERSION_RE = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(text: str) -> tuple[int, ...]:
    """The leading dotted integers of a version string.

    ``0.28.1rc1.dev345+g4cc0cb6f7`` is ``(0, 28, 1)``; anything with no
    leading number at all is ``()``, meaning "unknown".
    """
    match = _VERSION_RE.match(str(text).strip().lstrip("vV"))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


# ── Base class ───────────────────────────────────────────────────────────────


class Engine:
    """Base class for engine plugins."""

    name: str = ""

    min_framework_version: tuple[int, ...] = ()
    """Oldest framework version that understands what this engine renders.

    Empty means the renderer makes no version demand.
    """

    def __init__(self, spec: EngineSpec):
        self.spec = spec

    # -- declarative accessors, all driven by engine.yaml ------------------

    def default_image(self) -> str:
        """Fully qualified default image reference for this engine."""
        return self.spec.image_ref

    def cache_mounts(self) -> list[str]:
        """Host cache paths this engine wants mounted into the container."""
        return list(self.spec.runtime.cache_mounts)

    def container_profile(self) -> dict[str, Any]:
        """Container resource profile (privileged, shm, ulimits, devices…)."""
        return self.spec.runtime.container.model_dump()

    def readiness_path(self) -> str:
        return self.spec.runtime.readiness

    def models_path(self) -> str | None:
        """Endpoint that reports the served model id, if the engine has one.

        Distinct from :meth:`readiness_path`: SGLang answers readiness on
        ``/health`` but only names the model on ``/v1/models``.
        """
        return self.spec.runtime.models_endpoint

    def metrics_path(self) -> str | None:
        return self.spec.runtime.metrics

    def api_port(self) -> int:
        return self.spec.runtime.ports.api

    def rendezvous_port(self) -> int | None:
        return self.spec.runtime.ports.rendezvous

    def supports_mods(self) -> bool:
        return bool(self.spec.capabilities.mods)

    def framework_version(self) -> str:
        """The serving framework version in this engine's image, if declared.

        Falls back to ``sources.<engine>.version`` so a spec that pins the
        framework as a source need not repeat itself. Empty means unknown.
        """
        if self.spec.framework_version:
            return str(self.spec.framework_version)
        source = self.spec.sources.get(self.name)
        if isinstance(source, dict) and source.get("version"):
            return str(source["version"])
        return ""

    def version_supported(self) -> tuple[bool, str]:
        """Whether the image can run what this engine renders.

        ``(False, reason)`` refuses the launch. ``(True, reason)`` with a
        non-empty reason is a warning: the spec declares no framework version,
        so the demand can be stated but not checked.
        """
        required = self.min_framework_version
        if not required:
            return True, ""
        wanted = ".".join(str(p) for p in required)
        declared = self.framework_version()
        found = parse_version(declared)
        if not found:
            return True, (
                f"engine '{self.spec.key}' declares no {self.name} version, so "
                f"it cannot be checked against the {self.name} >= {wanted} the "
                "rendered launch flags need"
            )
        if found < required:
            return False, (
                f"engine '{self.spec.key}' carries {self.name} {declared}, but "
                f"the rendered launch flags (--nnodes/--node-rank/--master-addr"
                f"/--master-port) need {self.name} >= {wanted}; pick a newer "
                "engine image or variant"
            )
        return True, ""

    def supports_size(self, node_count: int) -> tuple[bool, str]:
        """Whether this engine claims the topology size; else why not.

        The three ``capabilities`` flags are the engine's own claim, taken
        from its ``engine.yaml``, and this is the only place that reads them.
        A claim is about rendering and published guidance, never about
        hardware verification — an engine that renders four ranks correctly
        has still never been run on four machines here.

        Sizes above :data:`MAX_CLUSTER_NODES` are refused for every engine,
        whatever it claims, because there is no published configuration to
        render against.
        """
        if node_count < 1:
            return False, "a deployment needs at least one node"
        caps = self.spec.capabilities
        if node_count == 1:
            if not caps.solo:
                return False, (
                    f"engine '{self.spec.key}' does not support a single-node "
                    "deployment; it declares solo: false"
                )
            return True, ""
        if node_count > MAX_CLUSTER_NODES:
            return False, (
                f"{node_count} nodes is more than the {MAX_CLUSTER_NODES} this "
                "hardware has a published topology for: NVIDIA documents two "
                "nodes over the direct link and a three- or four-node mesh, "
                f"and nothing above four. Deploy on at most {MAX_CLUSTER_NODES} "
                "nodes"
            )
        if not caps.cluster:
            return False, (
                f"engine '{self.spec.key}' does not support multi-node "
                f"deployment; it declares cluster: false, so {node_count} "
                "nodes cannot be rendered. Deploy it on one node, or pick an "
                "engine variant that declares cluster support"
            )
        if node_count >= 3 and not caps.mesh:
            return False, (
                f"engine '{self.spec.key}' declares mesh: false, so it claims "
                f"only the two-node arrangement, not {node_count} nodes. "
                "Above two, NVIDIA's reference wants a QSFP switch or — at "
                "three nodes — a switchless ring cabling port 0 of one Spark "
                "into port 1 of the next; a daisy chain sustains 100G between "
                "each pair rather than 200G, so it is a different "
                "configuration rather than more of the same. Deploy on two "
                "nodes, or pick an engine variant that declares mesh support"
            )
        return True, ""

    def pinning_env(
        self, eth_if: str, ib_if: str, node_count: int, mesh: bool = False
    ) -> dict[str, str]:
        """Interface pinning — the one thing that differs at a single node.

        Everything else about a launch is rendered identically at every size.
        This is not, and the gate belongs here rather than spread across the
        engines.

        ``NCCL_SOCKET_IFNAME`` and ``GLOO_SOCKET_IFNAME`` are find-or-fail: a
        collective told to use ``enp1s0f0np0`` dies if that interface is
        missing rather than choosing another. NCCL's selection code says so in
        as many words — ``// Specified by user : find or fail`` in
        ``src/misc/socket.cc`` — and its callers turn an empty result into
        ``WARN("Bootstrap : no socket interface found")``. ``NCCL_IB_HCA`` is
        *not* find-or-fail: a name that matches no device leaves NCCL with
        zero IB devices, which disables the IB plugin and silently falls back
        to TCP sockets at INFO level. That is worse than an error, not better,
        which is why the pre-flight checks the names.

        Pinning the fabric is right when ranks talk across machines and wrong
        when there is only one machine, which never touches the fabric at all.
        A solo deployment used to get none of these because its topology
        carried no nodes; now that a size-one topology carries a real node,
        the gate is what keeps them off that path.

        One variable is still worth setting alone: ``GLOO_SOCKET_IFNAME=lo``.
        Gloo otherwise calls ``gethostname()`` and resolves it to pick an
        interface (``ProcessGroupGloo.cpp``, ``createDefaultDevice``), warning
        and falling back to loopback when that does not resolve. Loopback is
        the right answer for a single node anyway — and only for a single
        node, which is why this sits behind the same gate.

        ``mesh`` adds the three NCCL settings upstream writes into ``.env``
        when it finds four CX7 ports up (``autodiscover.sh`` lines 186-190).
        They configure NCCL itself rather than the serving framework, so both
        engines get them, and a single-cable fabric must not: subnet-aware
        routing and a disabled NIC merge are corrections for a ring whose
        links land on different subnets, which a pair does not have.
        """
        if node_count <= 1:
            return {"GLOO_SOCKET_IFNAME": "lo"}
        env = self._fabric_env(eth_if, ib_if)
        if mesh:
            from spark_pulse.tools.discovery import MESH_NCCL_ENV

            env.update(MESH_NCCL_ENV)
        return env

    def _fabric_env(self, eth_if: str, ib_if: str) -> dict[str, str]:
        """Fabric pinning for this engine. Only called above one node."""
        env: dict[str, str] = {}
        if eth_if:
            env["NCCL_SOCKET_IFNAME"] = eth_if
            env["GLOO_SOCKET_IFNAME"] = eth_if
        if ib_if:
            env["NCCL_IB_HCA"] = ib_if
        return env

    def base_env(
        self,
        node_ip: str = "",
        eth_if: str = "",
        ib_if: str = "",
        node_count: int = 1,
        mesh: bool = False,
    ) -> dict[str, str]:
        """Environment shared by every rank. Subclasses extend this."""
        env = self.pinning_env(eth_if, ib_if, node_count, mesh)
        env.update(self.spec.runtime.env)
        return env

    # -- to implement ------------------------------------------------------

    def render(
        self,
        recipe: dict[str, Any],
        model: str | None = None,
        params: dict[str, Any] | None = None,
        extra_args: list[str] | None = None,
        topology: Topology | None = None,
        node_rank: int = 0,
    ) -> LaunchScript:
        raise NotImplementedError

    def supports(self, recipe: dict[str, Any]) -> tuple[bool, str]:
        """Whether this engine can run *recipe*; second item is the reason.

        Three signals, in order:

        * a top-level ``command`` template is written in one engine's flags and
          pins the recipe to it (a v1 recipe names no engine and is vLLM's);
        * an ``engines`` block (or the flattened list of engine names the API
          serves) enumerates the engines the recipe describes;
        * ``engine`` alone is only the *default* engine, so it pins nothing —
          except when the recipe says nothing else about engines at all.
        """
        command = str(recipe.get("command") or "").strip()
        pin = recipe.get("engine") or ("vllm" if command else None)
        if command and pin != self.name:
            return (
                False,
                (
                    f"recipe carries an engine-specific command for '{pin}' "
                    f"({pin} flags cannot run on {self.name})"
                ),
            )
        declared = self.declared_engines(recipe)
        if declared:
            if self.name not in declared:
                return False, f"recipe only declares engines: {', '.join(declared)}"
        elif pin and pin != self.name:
            return False, f"recipe names engine '{pin}'"
        return True, ""

    # -- helpers shared by concrete engines --------------------------------

    @staticmethod
    def declared_engines(recipe: dict[str, Any]) -> list[str]:
        """Engine names a recipe declares.

        A parsed v2 document carries ``engines`` as a mapping; the flattened
        payload the API serves carries it as a list of names, with the mapping
        kept under ``engine_specs``. Both are understood.
        """
        for key in ("engines", "engine_specs"):
            value = recipe.get(key)
            if isinstance(value, dict) and value:
                return sorted(value)
            if isinstance(value, (list, tuple)) and value:
                return [str(v) for v in value]
        return []

    def _engine_block(self, recipe: dict[str, Any]) -> dict[str, Any]:
        """This engine's per-engine override block, from either shape."""
        for key in ("engine_specs", "engines"):
            value = recipe.get(key)
            if isinstance(value, dict):
                block = value.get(self.name)
                if isinstance(block, dict):
                    return block
        return {}

    def block_mods(self, recipe: dict[str, Any]) -> list[str]:
        """Mods for this engine.

        A per-engine block wins outright, for the same reason ``_block_env``
        does: the flattened payload's top-level ``mods`` belongs to whichever
        engine the recipe defaults to.
        """
        block = self._engine_block(recipe)
        source = block.get("mods") if isinstance(block.get("mods"), list) else None
        if source is None and not block:
            source = recipe.get("mods")
        return [str(m) for m in (source or [])]

    def block_env(self, recipe: dict[str, Any]) -> dict[str, str]:
        """Public alias for :meth:`_block_env`, used by the deploy runtime."""
        return self._block_env(recipe)

    def _block_args(self, recipe: dict[str, Any]) -> str:
        """The engine-specific argument tail, normalised to one string."""
        args = self._engine_block(recipe).get("args")
        if args is None:
            args = recipe.get("args")
        if args is None:
            return ""
        if isinstance(args, (list, tuple)):
            return " ".join(str(a) for a in args)
        return str(args)

    def _block_env(self, recipe: dict[str, Any]) -> dict[str, str]:
        """Environment for this engine.

        A per-engine block wins outright rather than merging: the flattened
        payload's top-level ``env`` belongs to whichever engine the recipe
        defaults to, so merging it would leak one engine's variables into
        another's launch.
        """
        block = self._engine_block(recipe)
        source = block.get("env") if isinstance(block.get("env"), dict) else None
        if source is None and not block:
            source = recipe.get("env")
        return {str(k): str(v) for k, v in (source or {}).items()}

    def _resolved_params(
        self, recipe: dict[str, Any], params: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Recipe defaults merged with caller overrides (overrides win)."""
        merged: dict[str, Any] = dict(recipe.get("defaults") or {})
        merged.update(recipe.get("params") or {})
        for key, value in (params or {}).items():
            if value is not None:
                merged[key] = value
        return merged

    def _resolved_model(self, recipe: dict[str, Any], model: str | None) -> str:
        resolved = model or recipe.get("model") or ""
        if not resolved or resolved == "unknown":
            raise EngineError("no model: the recipe has none and none was supplied")
        return str(resolved)

    def _flag_args(
        self,
        params: dict[str, Any],
        order: tuple[str, ...] = (),
        only: tuple[str, ...] | None = None,
    ) -> list[str]:
        """Map engine-neutral params onto this engine's CLI flags.

        ``order`` puts the named keys first; anything else declared in
        ``param_flags`` follows in spec order. ``only`` restricts the output to
        the named keys.
        """
        flags = self.spec.runtime.param_flags
        keys = [k for k in order if k in flags]
        keys += [k for k in flags if k not in order]
        args: list[str] = []
        for key in keys:
            if only is not None and key not in only:
                continue
            value = params.get(key)
            if value is None or value == "":
                continue
            args.extend([flags[key], str(value)])
        return args

    @staticmethod
    def _quote_extra(extra_args: list[str] | None) -> str:
        if not extra_args:
            return ""
        return " ".join(shlex.quote(str(a)) for a in extra_args)

    @staticmethod
    def _env_exports(env: dict[str, str]) -> list[str]:
        return [f'export {k}="{v}"' for k, v in env.items()]

    def _script(self, env: dict[str, str], command: str) -> str:
        lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
        lines.extend(self._env_exports(env))
        if env:
            lines.append("")
        lines.append(command)
        return "\n".join(lines) + "\n"
