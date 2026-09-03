"""Engine abstraction — spec model, launch script container and base class.

An *engine* is a serving framework (vLLM, SGLang). Everything framework
specific lives here or in the concrete engine module; the runtime and cluster
code stay engine agnostic.

The declarative half of an engine comes from an ``engine.yaml`` published by
the ``spark-pulse-engine`` repo (see ``spark-engine.schema.json``); it is parsed
into :class:`EngineSpec`. The Python subclass only holds rendering logic.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    tag: str | None = None
    digest: str | None = None
    legacy_tags: list[str] = Field(default_factory=list)
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

    def address(self) -> str:
        return self.ip or self.host


@dataclass
class Topology:
    """How many nodes take part and which one is the head."""

    nodes: list[NodeInfo] = field(default_factory=list)

    @property
    def size(self) -> int:
        return max(1, len(self.nodes))

    @property
    def is_solo(self) -> bool:
        return self.size <= 1

    @property
    def head(self) -> NodeInfo | None:
        return self.nodes[0] if self.nodes else None

    def node(self, rank: int) -> NodeInfo | None:
        if 0 <= rank < len(self.nodes):
            return self.nodes[rank]
        return None

    @classmethod
    def solo(cls, node: NodeInfo | None = None) -> Topology:
        return cls(nodes=[node] if node else [])


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


# ── Base class ───────────────────────────────────────────────────────────────


class Engine:
    """Base class for engine plugins."""

    name: str = ""

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

    def metrics_path(self) -> str | None:
        return self.spec.runtime.metrics

    def api_port(self) -> int:
        return self.spec.runtime.ports.api

    def rendezvous_port(self) -> int | None:
        return self.spec.runtime.ports.rendezvous

    def supports_mods(self) -> bool:
        return bool(self.spec.capabilities.mods)

    def base_env(
        self,
        node_ip: str = "",
        eth_if: str = "",
        ib_if: str = "",
    ) -> dict[str, str]:
        """Environment shared by every rank. Subclasses extend this."""
        return dict(self.spec.runtime.env)

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
        """Whether this engine can run *recipe*; second item is the reason."""
        return True, ""

    # -- helpers shared by concrete engines --------------------------------

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
