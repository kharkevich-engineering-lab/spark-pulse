"""Engine registry — bundled defaults plus optional OCI engine indexes.

Specs come from two places:

1. **Bundled defaults** (``spark_pulse/engines/defaults/*.yaml``) — always
   available, no network, loaded eagerly.
2. **Engine indexes** — OCI artifacts named by ``engine_indexes`` in the
   config. Each holds an ``index.yaml``::

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

   Index results are cached on disk with a TTL. Fetching **never** happens on
   startup and never blocks it: the registry serves whatever is cached, and a
   refresh is an explicit call (``POST /api/engines/refresh``) or a lazy
   TTL-expired top-up. In simulation mode the network is skipped entirely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from spark_pulse.config import config
from spark_pulse.engines.base import Engine, EngineError, EngineSpec
from spark_pulse.engines.sglang import SglangEngine
from spark_pulse.engines.vllm import VllmEngine

logger = logging.getLogger(__name__)

DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
CACHE_DIR = Path.home() / ".cache" / "spark-pulse" / "engines"

ENGINE_CLASSES: dict[str, type[Engine]] = {
    "vllm": VllmEngine,
    "sglang": SglangEngine,
}

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
        if entry.get("legacy_tags") and not payload.get("legacy_tags"):
            payload["legacy_tags"] = entry["legacy_tags"]
        payload["source"] = source
        try:
            specs.append(EngineSpec.model_validate(payload))
        except ValidationError as exc:  # pragma: no cover - defensive
            logger.warning("Skipping engine entry in %s: %s", source, exc)
    return specs


def load_bundled_specs(defaults_dir: Path | None = None) -> list[EngineSpec]:
    """Parse every bundled ``engine.yaml``."""
    directory = defaults_dir or DEFAULTS_DIR
    specs: list[EngineSpec] = []
    if not directory.is_dir():
        return specs
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Skipping bundled engine %s: %s", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        data["source"] = "bundled"
        try:
            specs.append(EngineSpec.model_validate(data))
        except ValidationError as exc:
            logger.warning("Skipping bundled engine %s: %s", path.name, exc)
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
    """Bundled engine specs plus whatever the configured indexes provide."""

    def __init__(
        self,
        defaults_dir: Path | None = None,
        cache_dir: Path | None = None,
        cache_ttl: int | None = None,
    ):
        self._defaults_dir = defaults_dir
        self._cache_dir = cache_dir or CACHE_DIR
        self._cache_ttl = cache_ttl
        self._lock = threading.RLock()
        self._bundled: list[EngineSpec] = []
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
        """Reload bundled defaults and any cached index payloads (no network)."""
        with self._lock:
            self._bundled = load_bundled_specs(self._defaults_dir)
            self._indexed = {}
            self._fetched_at = {}
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
                "reason": "simulation mode: bundled engines only",
                "indexes": results,
                "engines": len(self.list()),
            }
        now = time.time()
        for ref in self.index_refs:
            if not force and now - self._fetched_at.get(ref, 0) < self.cache_ttl:
                results.append(
                    {
                        "ref": ref,
                        "status": "cached",
                        "engines": len(self._indexed.get(ref, [])),
                    }
                )
                continue
            try:
                data = fetch_index(ref)
            except Exception as exc:
                logger.warning("Engine index %s refresh failed: %s", ref, exc)
                results.append({"ref": ref, "status": "error", "error": str(exc)})
                continue
            if data is None:
                results.append(
                    {"ref": ref, "status": "error", "error": "no index.yaml"}
                )
                continue
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

    def refresh_if_stale(self) -> None:
        """Top up expired indexes; safe to call from a request path."""
        if _is_simulation():
            return
        now = time.time()
        if any(
            now - self._fetched_at.get(ref, 0) >= self.cache_ttl
            for ref in self.index_refs
        ):
            self.refresh(force=False)

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
            path.write_text(
                json.dumps({"ref": ref, "fetched_at": fetched_at, "index": data})
            )
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("Cannot cache engine index %s: %s", ref, exc)

    # -- lookup ------------------------------------------------------------

    def list(self) -> list[EngineSpec]:
        """All known specs; an index entry shadows the bundled one it matches."""
        with self._lock:
            merged: dict[str, EngineSpec] = {s.key: s for s in self._bundled}
            for ref in self.index_refs:
                for spec in self._indexed.get(ref, []):
                    merged[spec.key] = spec
        return [merged[k] for k in sorted(merged)]

    def get(self, engine: str, variant: str = "default") -> EngineSpec:
        for spec in self.list():
            if spec.engine == engine and spec.variant == variant:
                return spec
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

    def engine(self, engine: str, variant: str = "default") -> Engine:
        """Instantiate the engine plugin for ``engine/variant``."""
        spec = self.get(engine, variant)
        cls = ENGINE_CLASSES.get(spec.engine)
        if cls is None:
            raise EngineNotFound(f"no engine plugin implements '{spec.engine}'")
        return cls(spec)


# ── Process-wide registry ────────────────────────────────────────────────────

_registry: EngineRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> EngineRegistry:
    """Return (creating on first use) the process-wide registry."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = EngineRegistry()
        return _registry


def reset_registry() -> None:
    """Drop the cached registry — used by tests and after a settings change."""
    global _registry
    with _registry_lock:
        _registry = None
