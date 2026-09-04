"""Engine plugins and the engine registry.

Engines are not routed through ``spark_pulse.tools`` because there is nothing
to simulate: rendering is pure, and the registry falls back to bundled specs
whenever the network is unavailable or simulation mode is on.
"""

from spark_pulse.engines.base import (
    Engine,
    EngineCapabilities,
    EngineContainer,
    EngineError,
    EngineMultiNode,
    EnginePorts,
    EngineRuntime,
    EngineSpec,
    EngineVerification,
    LaunchScript,
    NodeInfo,
    Topology,
)
from spark_pulse.engines.registry import (
    ENGINE_CLASSES,
    EngineNotFound,
    EngineRegistry,
    get_registry,
    reset_registry,
)
from spark_pulse.engines.sglang import SglangEngine
from spark_pulse.engines.vllm import VllmEngine

__all__ = [
    "ENGINE_CLASSES",
    "Engine",
    "EngineCapabilities",
    "EngineContainer",
    "EngineError",
    "EngineMultiNode",
    "EngineNotFound",
    "EnginePorts",
    "EngineRegistry",
    "EngineRuntime",
    "EngineSpec",
    "EngineVerification",
    "LaunchScript",
    "NodeInfo",
    "SglangEngine",
    "Topology",
    "VllmEngine",
    "get_registry",
    "reset_registry",
]
