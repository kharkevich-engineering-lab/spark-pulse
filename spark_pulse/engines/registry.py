"""Engine registry — the configured engine indexes are the registry.

Specs come from the OCI artifacts named by ``engine_indexes`` in the config.
Each holds an ``index.yaml``::

       apiVersion: spark-pulse.io/v1
       kind: EngineIndex
       engines:
         - id: vllm-default
           engine: vllm
           variant: default
           version: 0.1.0
           image: ghcr.io/.../vllm
           tag: "0.1.0"
           ref: ghcr.io/.../vllm:0.1.0
           digest: sha256:...
           spec: {...}          # a full engine.yaml

Nothing about an engine ships in the wheel: adding one, or a variant, is a
change to the index, and a control plane picks it up on its next refresh
without a release. Which class renders a spec is :func:`engine_class`'s
answer, and it never refuses an engine name it has not heard of.

Index results are cached on disk, and the cache is what a restart serves —
including one with no network. A fetch never blocks startup: a registry with
an uncached or expired index starts a background refresh when it is created
(at startup and after the settings change it), and ``POST
/api/engines/refresh`` is the explicit one. Until the first fetch lands there
are no engines, and a lookup says so rather than "unknown engine".

Simulation has no network, so it reads a fixed index instead — the specs in
``spark_pulse/mock/engines`` — which is also what the tests render against.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from spark_pulse.config import config
from spark_pulse.engines.base import Engine, EngineError, EngineSpec
from spark_pulse.engines.llama_cpp import LlamaCppEngine
from spark_pulse.engines.sglang import SglangEngine
from spark_pulse.engines.solo import (
    AtlasEngine,
    GenericEngine,
    ModularMaxEngine,
    SoloEngine,
    TrtllmEngine,
)
from spark_pulse.engines.vllm import VllmEngine

logger = logging.getLogger(__name__)

#: Simulation's engine index: one ``engine.yaml`` per file. Read only in
#: simulation (and by tests that name it); a real control plane's engines are
#: its indexes'.
SIMULATION_SPECS_DIR = Path(__file__).resolve().parent.parent / "mock" / "engines"
SIMULATION_SOURCE = "simulation"
CACHE_DIR = Path.home() / ".cache" / "spark-pulse" / "engines"

ENGINE_CLASSES: dict[str, type[Engine]] = {
    "vllm": VllmEngine,
    "sglang": SglangEngine,
    # Keyed by engine name, so every *variant* of an engine is rendered by
    # the same class: ``llama-cpp/default`` is solo and ``llama-cpp/prism``
    # spans nodes over RPC, and which of the two happens is the spec's
    # ``multi_node.style``, never a second entry here.
    "llama-cpp": LlamaCppEngine,
    # Single-node engines, all rendered by the same class: see
    # ``spark_pulse.engines.solo`` for why one is enough for the rest.
    "trtllm": TrtllmEngine,
    "modular-max": ModularMaxEngine,
    "atlas": AtlasEngine,
}

#: Every renderer a spec may name with ``runtime.renderer``. The engine's own
#: name is the first thing asked (:data:`ENGINE_CLASSES`), so a built-in needs
#: no ``renderer`` line; an engine the index adds later says which dialect it
#: speaks — ``vllm`` for a vLLM fork, ``solo`` for one process on one node —
#: and runs without a control-plane release.
RENDERERS: dict[str, type[Engine]] = {**ENGINE_CLASSES, "solo": SoloEngine}

#: A ``multi_node.style`` that is itself enough to pick the renderer, for a
#: spec that names neither a known engine nor a renderer.
STYLE_RENDERERS: dict[str, type[Engine]] = {"llama-rpc": LlamaCppEngine}


def engine_class(spec: EngineSpec) -> type[Engine]:
    """The class that renders ``spec``. Never refuses an unknown engine name.

    In order: the renderer the spec names, the class registered for its
    engine name, the class its multi-node style implies, and otherwise
    :class:`GenericEngine` — the spec-driven solo renderer, which refuses a
    topology above one node at plan time because it has no rendezvous to
    render. Only a renderer the spec *names* and this build lacks is an
    error, and it says so, since rendering it as something else would launch
    a command nobody wrote.
    """
    renderer = (spec.runtime.renderer or "").strip()
    if renderer:
        cls = RENDERERS.get(renderer)
        if cls is None:
            raise EngineNotFound(
                f"engine '{spec.key}' asks for the '{renderer}' renderer, which "
                f"this control plane does not have (has: "
                f"{', '.join(sorted(RENDERERS))}); upgrade spark-pulse"
            )
        return cls
    cls = ENGINE_CLASSES.get(spec.engine) or STYLE_RENDERERS.get(
        spec.runtime.multi_node.style
    )
    return cls or GenericEngine


#: How long a failed fetch waits before the background top-up tries again.
#: Short, because until a fresh install's first fetch lands it has no engines.
RETRY_SECONDS = 60

INDEX_API_VERSION = "spark-pulse.io/v1"
INDEX_KIND = "EngineIndex"


class EngineNotFound(EngineError):
    """Raised when no spec matches the requested engine/variant."""


def _is_simulation() -> bool:
    from spark_pulse.tools import is_simulation

    return is_simulation()


# ── Index parsing ────────────────────────────────────────────────────────────


def parse_index(data: dict[str, Any], source: str) -> list[EngineSpec]:
    """Turn an ``index.yaml`` payload into specs, skipping bad entries."""
    if not isinstance(data, dict):
        return []
    kind = data.get("kind")
    if kind and kind != INDEX_KIND:
        logger.warning("Ignoring engine index %s: kind=%s", source, kind)
        return []

    specs: list[EngineSpec] = []
    for entry in data.get("engines") or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("spec")
        payload: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        if not payload:
            # Index entries may be metadata-only; synthesise a minimal spec.
            if not entry.get("engine"):
                continue
            payload = {
                "engine": entry["engine"],
                "variant": entry.get("variant", "default"),
                "version": entry.get("version", "0.0.0"),
                "image": entry.get("image", ""),
            }
        for field_name in ("image", "version", "tag", "digest", "description"):
            if entry.get(field_name) and not payload.get(field_name):
                payload[field_name] = entry[field_name]
        if "available" in entry:
            payload["available"] = bool(entry["available"])
        # Index entries carry ``tag`` as the full ``image:version`` reference;
        # the spec wants the bare tag.
        tag = payload.get("tag")
        image = payload.get("image") or ""
        if isinstance(tag, str) and image and tag.startswith(image + ":"):
            payload["tag"] = tag[len(image) + 1 :]
        if entry.get("legacy_tags") and not payload.get("legacy_tags"):
            payload["legacy_tags"] = entry["legacy_tags"]
        payload["source"] = source
        try:
            specs.append(EngineSpec.model_validate(payload))
        except ValidationError as exc:  # pragma: no cover - defensive
            logger.warning("Skipping engine entry in %s: %s", source, exc)
    return specs


def load_spec_dir(
    directory: Path | None = None, source: str = SIMULATION_SOURCE
) -> list[EngineSpec]:
    """Parse every ``engine.yaml`` in ``directory`` (simulation's by default)."""
    directory = directory or SIMULATION_SPECS_DIR
    specs: list[EngineSpec] = []
    if not directory.is_dir():
        return specs
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Skipping engine spec %s: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        data["source"] = source
        try:
            specs.append(EngineSpec.model_validate(data))
        except ValidationError as exc:
            logger.warning("Skipping engine spec %s: %s", path.name, exc)
    return specs


# ── Index fetching ───────────────────────────────────────────────────────────


def _split_ref(ref: str) -> tuple[str, str]:
    """Split ``repo[:tag]`` into ``(repo, tag)``."""
    head, sep, tail = ref.rpartition(":")
    if sep and "/" not in tail:
        return head, tail
    return ref, "latest"


def _read_index_file(path: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Cannot read engine index %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def fetch_index(ref: str) -> dict[str, Any] | None:
    """Fetch one engine index. A local path is read directly, else OCI."""
    local = Path(ref.removeprefix("file://")).expanduser()
    if local.exists():
        if local.is_dir():
            for name in ("index.yaml", "index.yml"):
                candidate = local / name
                if candidate.exists():
                    return _read_index_file(candidate)
            return None
        return _read_index_file(local)
    return _fetch_index_oci(ref)


def _fetch_index_oci(ref: str) -> dict[str, Any] | None:
    """Pull an OCI artifact and return the ``index.yaml`` it carries."""
    from spark_pulse.tools.oci_registry import _oras_client

    url, tag = _split_ref(ref)
    client = _oras_client()
    manifest = client.get_manifest(f"{url}:{tag}")

    manifests = manifest.get("manifests") or []
    targets = [f"{url}@{m['digest']}" for m in manifests if m.get("digest")] or [
        f"{url}:{tag}"
    ]

    with tempfile.TemporaryDirectory(prefix="spark-pulse-engines-") as tmp:
        tmpdir = Path(tmp)
        for target in targets:
            try:
                child = manifest if target.endswith(f":{tag}") else None
                if child is None:
                    child = client.get_manifest(target)
            except Exception as exc:
                logger.warning("Engine index %s: manifest fetch failed: %s", ref, exc)
                continue
            for layer in child.get("layers") or []:
                digest = layer.get("digest")
                if not digest:
                    continue
                title = (layer.get("annotations") or {}).get(
                    "org.opencontainers.image.title", ""
                )
                name = Path(title).name or f"layer-{digest[-12:]}.yaml"
                if not name.endswith((".yaml", ".yml")):
                    continue
                dest = tmpdir / name
                try:
                    client.download_blob(target, digest, str(dest))
                except Exception as exc:
                    logger.warning(
                        "Engine index %s: blob %s failed: %s", ref, name, exc
                    )
                    continue
                data = _read_index_file(dest)
                if isinstance(data, dict) and data.get("engines") is not None:
                    return data
    return None


# ── Registry ─────────────────────────────────────────────────────────────────


class EngineRegistry:
    """What the configured engine indexes provide, served from their cache.

    ``specs_dir`` is a fixed index read from disk — simulation's, or a test's.
    Left unset, it is simulation's in simulation and nothing otherwise.
    """

    def __init__(
        self,
        specs_dir: Path | None = None,
        cache_dir: Path | None = None,
        cache_ttl: int | None = None,
    ):
        self._specs_dir = specs_dir
        self._cache_dir = cache_dir or CACHE_DIR
        self._explicit_cache = cache_dir is not None
        self._cache_ttl = cache_ttl
        self._lock = threading.RLock()
        # One fetch at a time: the background top-up and a request's
        # an explicit refresh would otherwise pull the same index twice and
        # race on its cache file.
        self._refresh_lock = threading.Lock()
        self._attempted_at: dict[str, float] = {}
        self._errors: dict[str, str] = {}
        self._fixed: list[EngineSpec] = []
        self._indexed: dict[str, list[EngineSpec]] = {}
        self._fetched_at: dict[str, float] = {}
        self.reload()

    # -- loading -----------------------------------------------------------

    @property
    def cache_ttl(self) -> int:
        if self._cache_ttl is not None:
            return self._cache_ttl
        return config.engine_index_cache_ttl_seconds

    @property
    def index_refs(self) -> list[str]:
        return list(config.engine_indexes)

    def reload(self) -> None:
        """Reload the fixed specs and any cached index payloads (no network)."""
        with self._lock:
            if self._specs_dir is not None:
                self._fixed = load_spec_dir(self._specs_dir)
            elif _is_simulation():
                self._fixed = load_spec_dir()
            else:
                self._fixed = []
            self._indexed = {}
            self._fetched_at = {}
            # Simulation renders against its fixed index alone: a real
            # control plane's cache on the same machine would shadow it. A
            # test that names a cache directory still reads that one.
            if _is_simulation() and not self._explicit_cache:
                return
            for ref in self.index_refs:
                cached = self._read_cache(ref)
                if cached is not None:
                    self._indexed[ref] = parse_index(cached.get("index", {}), ref)
                    self._fetched_at[ref] = float(cached.get("fetched_at", 0))

    def refresh(self, force: bool = True) -> dict[str, Any]:
        """Re-fetch every configured index. Errors are reported, not raised."""
        results: list[dict[str, Any]] = []
        if _is_simulation():
            return {
                "refreshed": False,
                "reason": "simulation mode: the simulated engine index only",
                "indexes": results,
                "engines": len(self.list()),
            }
        with self._refresh_lock:
            return self._refresh_locked(force)

    def _refresh_locked(self, force: bool) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        now = time.time()
        for ref in self.index_refs:
            # Read under the refresh lock, so a call that waited behind
            # another's fetch sees that fetch and skips.
            fresh = now - self._fetched_at.get(ref, 0) < self.cache_ttl
            backing_off = now - self._attempted_at.get(ref, 0) < RETRY_SECONDS
            if not force and (fresh or backing_off):
                results.append(
                    {
                        "ref": ref,
                        # Backing off: tried and failed within RETRY_SECONDS.
                        "status": "cached" if fresh else "retry_later",
                        "engines": len(self._indexed.get(ref, [])),
                    }
                )
                continue
            self._attempted_at[ref] = now
            try:
                data = fetch_index(ref)
            except Exception as exc:
                logger.warning("Engine index %s refresh failed: %s", ref, exc)
                self._errors[ref] = str(exc) or type(exc).__name__
                results.append({"ref": ref, "status": "error", "error": str(exc)})
                continue
            if data is None:
                self._errors[ref] = "no index.yaml"
                results.append(
                    {"ref": ref, "status": "error", "error": "no index.yaml"}
                )
                continue
            self._errors.pop(ref, None)
            specs = parse_index(data, ref)
            with self._lock:
                self._indexed[ref] = specs
                self._fetched_at[ref] = now
            self._write_cache(ref, data, now)
            results.append({"ref": ref, "status": "ok", "engines": len(specs)})
        return {
            "refreshed": True,
            "indexes": results,
            "engines": len(self.list()),
        }

    def needs_refresh(self) -> bool:
        """Whether an index is uncached or stale and not tried in the last
        :data:`RETRY_SECONDS` — so a failed fetch is retried, but not on
        every request."""
        now = time.time()
        return any(
            now - self._fetched_at.get(ref, 0) >= self.cache_ttl
            and now - self._attempted_at.get(ref, 0) >= RETRY_SECONDS
            for ref in self.index_refs
        )

    def refresh_in_background(self) -> threading.Thread | None:
        """Top up stale indexes on a thread; never blocks, never raises."""
        if _is_simulation() or self._refresh_lock.locked():
            return None
        if not self.needs_refresh():
            return None

        def run() -> None:
            try:
                self.refresh(force=False)
            except Exception as exc:  # pragma: no cover - refresh reports its own
                logger.warning("Engine index background refresh failed: %s", exc)

        thread = threading.Thread(target=run, name="engine-index-refresh", daemon=True)
        thread.start()
        return thread

    def refresh_if_stale(self) -> None:
        """Top up expired indexes without making a request wait for the network.

        The background top-up, with its backoff: an unreachable index is a
        normal state now that the indexes are the only source of engines, and
        a page must show the last error rather than hang on the next fetch.
        """
        self.refresh_in_background()

    def index_status(self) -> list[dict[str, Any]]:
        """Per configured index: what it gave, when, and the last failure.

        ``error`` is the most recent fetch's and is cleared by the next one
        that succeeds; an index that failed but has a cached copy still serves
        that copy, which is why ``engines`` and ``error`` can both be set.
        """
        with self._lock:
            return [
                {
                    "ref": ref,
                    "engines": len(self._indexed.get(ref, [])),
                    "fetched_at": self._fetched_at.get(ref) or None,
                    "error": self._errors.get(ref),
                }
                for ref in self.index_refs
            ]

    # -- disk cache --------------------------------------------------------

    def _cache_path(self, ref: str) -> Path:
        key = hashlib.sha256(ref.encode()).hexdigest()[:16]
        return self._cache_dir / f"{key}.json"

    def _read_cache(self, ref: str) -> dict[str, Any] | None:
        path = self._cache_path(ref)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, ref: str, data: dict[str, Any], fetched_at: float) -> None:
        path = self._cache_path(ref)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Whole or not at all: a reader must never parse half a file and
            # take it for no cache.
            tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(
                json.dumps({"ref": ref, "fetched_at": fetched_at, "index": data})
            )
            os.replace(tmp, path)
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Cannot cache engine index %s: %s", ref, exc)

    # -- lookup ------------------------------------------------------------

    def list(self) -> list[EngineSpec]:
        """All known specs; a later index shadows an earlier one's entry."""
        with self._lock:
            merged: dict[str, EngineSpec] = {s.key: s for s in self._fixed}
            for ref in self.index_refs:
                for spec in self._indexed.get(ref, []):
                    merged[spec.key] = spec
        return [merged[k] for k in sorted(merged)]

    def get(self, engine: str, variant: str = "default") -> EngineSpec:
        specs = self.list()
        for spec in specs:
            if spec.engine == engine and spec.variant == variant:
                return spec
        if not specs:
            raise EngineNotFound(
                f"unknown engine '{engine}/{variant}': no engine index has been "
                "fetched yet — refresh the engine index (Library → Engines) or "
                "check engine_indexes in settings"
            )
        raise EngineNotFound(f"unknown engine '{engine}/{variant}'")

    def has(self, engine: str, variant: str = "default") -> bool:
        try:
            self.get(engine, variant)
        except EngineNotFound:
            return False
        return True

    def resolve_legacy_tag(self, tag: str) -> EngineSpec:
        """Map a v1 ``container:`` tag such as ``vllm-node`` onto a spec."""
        for spec in self.list():
            if tag in spec.legacy_tags:
                return spec
        raise EngineNotFound(f"no engine claims the legacy tag '{tag}'")

    def enabled(self, engine: str) -> bool:
        return config.engine_enabled(engine)

    def usable(self, spec: EngineSpec) -> bool:
        """Whether this engine can actually run a deployment.

        An engine the index marks unavailable has no image in the registry, so
        offering it only produces a 403 at pull time.
        """
        return spec.available and self.enabled(spec.engine)

    def select(
        self,
        request_override: str | None = None,
        recipe_engine: str | None = None,
        default_engine: str | None = None,
    ) -> tuple[str, str]:
        """Pick ``(engine, variant)``: request > recipe > config default.

        Either component may be given as ``engine/variant``.
        """
        for candidate in (request_override, recipe_engine, default_engine):
            if not candidate:
                continue
            engine, _, variant = str(candidate).partition("/")
            variant = variant or "default"
            spec = self.get(engine, variant)
            if not self.enabled(spec.engine):
                raise EngineError(f"engine '{spec.engine}' is disabled")
            return spec.engine, spec.variant
        engine, _, variant = config.default_engine.partition("/")
        spec = self.get(engine, variant or "default")
        return spec.engine, spec.variant

    def select_for_recipe(
        self,
        recipe: dict[str, Any],
        engine: str | None = None,
        variant: str | None = None,
    ) -> tuple[str, str]:
        """:meth:`select` for a deploy of ``recipe``: request, recipe, default.

        A recipe may pin a variant (``engine: sglang/inkling-2-mini``). A
        request naming that same bare engine and no variant has not chosen
        another image, so the recipe's variant stands.
        """
        recipe_engine = recipe.get("engine")
        recipe_variant = recipe.get("variant")
        if recipe_engine and recipe_variant:
            if engine == recipe_engine and not variant:
                engine = None
            recipe_engine = f"{recipe_engine}/{recipe_variant}"
        override = engine
        if override and variant:
            override = f"{override}/{variant}"
        elif variant and not override:
            base = (recipe_engine or config.default_engine).partition("/")[0]
            override = f"{base}/{variant}"
        return self.select(
            request_override=override,
            recipe_engine=recipe_engine,
            default_engine=config.default_engine,
        )

    def variant_for(self, engine: str, preferred: str | None = None) -> str:
        """A variant of ``engine`` to ask a question of: ``preferred``, else
        ``default``, else the first one the registry holds."""
        variants = [s.variant for s in self.list() if s.engine == engine]
        for candidate in (preferred, "default"):
            if candidate and candidate in variants:
                return candidate
        return variants[0] if variants else (preferred or "default")

    def engine(self, engine: str, variant: str = "default") -> Engine:
        """Instantiate the engine plugin for ``engine/variant``."""
        spec = self.get(engine, variant)
        return engine_class(spec)(spec)


# ── Process-wide registry ────────────────────────────────────────────────────

_registry: EngineRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> EngineRegistry:
    """Return (creating on first use) the process-wide registry."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = EngineRegistry()
        registry = _registry
    # Every use is a chance to top up: the first fetch of a fresh install (the
    # lifespan asks at startup), an index a settings change just named, and a
    # fetch that failed — retried no sooner than RETRY_SECONDS. Cheap when
    # nothing is due, and it never blocks the caller.
    registry.refresh_in_background()
    return registry


def reset_registry() -> None:
    """Drop the cached registry — used by tests and after a settings change."""
    global _registry
    with _registry_lock:
        _registry = None
