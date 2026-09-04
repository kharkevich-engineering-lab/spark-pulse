"""Engine registry: bundled defaults, index loading, legacy tags, selection."""

import textwrap

import pytest

from spark_pulse.engines import (
    EngineNotFound,
    EngineRegistry,
    SglangEngine,
    VllmEngine,
)
from spark_pulse.engines.registry import (
    load_bundled_specs,
    parse_index,
)

FAKE_INDEX = textwrap.dedent(
    """
    apiVersion: spark-pulse.io/v1
    kind: EngineIndex
    engines:
      - id: vllm-default
        engine: vllm
        variant: default
        version: 9.9.9
        image: ghcr.io/example/vllm
        tag: "9.9.9"
        ref: ghcr.io/example/vllm:9.9.9
        digest: sha256:deadbeef
        legacy_tags: [vllm-node]
        description: indexed vllm
        capabilities: {mods: true}
        spec:
          schema_version: "1"
          engine: vllm
          variant: default
          image: ghcr.io/example/vllm
          version: 9.9.9
          legacy_tags: [vllm-node]
          runtime:
            serve: vllm serve
            readiness: /v1/models
            ports: {api: 8000, rendezvous: 29501}
          capabilities: {mods: true, solo: true, cluster: true}
      - id: vllm-b12x
        engine: vllm
        variant: b12x
        version: 9.9.9
        image: ghcr.io/example/vllm-b12x
        spec:
          schema_version: "1"
          engine: vllm
          variant: b12x
          image: ghcr.io/example/vllm-b12x
          version: 9.9.9
          legacy_tags: [vllm-node-b12x]
          runtime:
            serve: vllm serve
            readiness: /v1/models
            ports: {api: 8000}
          capabilities: {mods: true}
    """
).strip()


@pytest.fixture
def index_file(tmp_path):
    path = tmp_path / "index.yaml"
    path.write_text(FAKE_INDEX)
    return path


@pytest.fixture
def registry(tmp_path):
    return EngineRegistry(cache_dir=tmp_path / "cache")


# ── Bundled defaults ─────────────────────────────────────────────────────────


def test_bundled_defaults_are_parsed():
    specs = {s.key: s for s in load_bundled_specs()}
    assert set(specs) == {"vllm/default", "sglang/default"}
    vllm = specs["vllm/default"]
    assert vllm.runtime.serve == "vllm serve"
    assert vllm.runtime.ports.api == 8000
    assert vllm.runtime.ports.rendezvous == 29501
    assert vllm.capabilities.mods is True
    assert vllm.source == "bundled"
    assert vllm.verified[0].nodes == 1


def test_registry_lists_bundled_engines(registry):
    assert [s.key for s in registry.list()] == ["sglang/default", "vllm/default"]


def test_get_and_has(registry):
    assert registry.get("vllm").version == "0.1.0"
    assert registry.has("sglang", "default") is True
    assert registry.has("vllm", "b12x") is False
    with pytest.raises(EngineNotFound, match="unknown engine"):
        registry.get("nope")


def test_engine_returns_the_right_plugin(registry):
    assert isinstance(registry.engine("vllm"), VllmEngine)
    assert isinstance(registry.engine("sglang"), SglangEngine)


def test_image_ref_prefers_digest(registry):
    spec = registry.get("vllm")
    assert spec.image_ref.endswith(":0.1.0")
    spec.digest = "sha256:abc"
    assert spec.image_ref.endswith("@sha256:abc")


def test_index_entry_without_digest_yields_a_well_formed_ref():
    from spark_pulse.engines.registry import parse_index

    image = "ghcr.io/example/spark-pulse-engine/vllm-b12x"
    specs = parse_index(
        {
            "engines": [
                {
                    "id": "vllm-b12x",
                    "engine": "vllm",
                    "variant": "b12x",
                    "version": "0.1.0",
                    "image": image,
                    "tag": f"{image}:0.1.0",
                    "ref": f"{image}:0.1.0",
                    "digest": None,
                    "spec": {
                        "schema_version": "1",
                        "engine": "vllm",
                        "variant": "b12x",
                        "image": image,
                        "version": "0.1.0",
                        "sources": {},
                        "runtime": {
                            "serve": "vllm serve",
                            "readiness": "/v1/models",
                            "ports": {"api": 8000},
                        },
                        "capabilities": {},
                    },
                }
            ]
        },
        "test-index",
    )
    assert len(specs) == 1
    assert specs[0].image_ref == f"{image}:0.1.0"


# ── Legacy tags ──────────────────────────────────────────────────────────────


def test_resolve_legacy_tag(registry):
    assert registry.resolve_legacy_tag("vllm-node").key == "vllm/default"


def test_resolve_unknown_legacy_tag(registry):
    with pytest.raises(EngineNotFound, match="legacy tag"):
        registry.resolve_legacy_tag("does-not-exist")


# ── Index loading ────────────────────────────────────────────────────────────


def test_parse_index_reads_entries_and_metadata(index_file):
    specs = parse_index(
        __import__("yaml").safe_load(index_file.read_text()), str(index_file)
    )
    keys = {s.key for s in specs}
    assert keys == {"vllm/default", "vllm/b12x"}
    default = next(s for s in specs if s.variant == "default")
    assert default.digest == "sha256:deadbeef"
    assert default.tag == "9.9.9"
    assert default.description == "indexed vllm"
    assert default.source == str(index_file)


def test_parse_index_rejects_wrong_kind():
    assert parse_index({"kind": "RecipeIndex", "engines": [{"engine": "x"}]}, "s") == []


def test_parse_index_skips_junk_entries():
    data = {"kind": "EngineIndex", "engines": ["nope", {}, {"engine": "vllm"}]}
    specs = parse_index(data, "s")
    assert [s.key for s in specs] == ["vllm/default"]


def test_refresh_loads_from_a_local_index_file(monkeypatch, tmp_path, index_file):
    monkeypatch.setattr("spark_pulse.engines.registry._is_simulation", lambda: False)
    monkeypatch.setattr(
        type(_config()), "engine_indexes", property(lambda self: [str(index_file)])
    )
    registry = EngineRegistry(cache_dir=tmp_path / "cache")
    # Before refresh only the bundled specs are known.
    assert registry.get("vllm").version == "0.1.0"

    result = registry.refresh()
    assert result["refreshed"] is True
    assert result["indexes"][0]["status"] == "ok"
    assert result["indexes"][0]["engines"] == 2

    # The indexed entry shadows the bundled one; the new variant shows up.
    assert registry.get("vllm").version == "9.9.9"
    assert registry.get("vllm").digest == "sha256:deadbeef"
    assert registry.get("vllm", "b12x").image == "ghcr.io/example/vllm-b12x"
    assert registry.resolve_legacy_tag("vllm-node-b12x").variant == "b12x"
    # sglang is untouched by the index.
    assert registry.get("sglang").version == "0.1.0"


def test_refresh_survives_a_missing_index(monkeypatch, tmp_path):
    monkeypatch.setattr("spark_pulse.engines.registry._is_simulation", lambda: False)
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(
        type(_config()), "engine_indexes", property(lambda self: [str(missing)])
    )

    def boom(ref):
        raise RuntimeError("registry down")

    monkeypatch.setattr("spark_pulse.engines.registry.fetch_index", boom)
    registry = EngineRegistry(cache_dir=tmp_path / "cache")
    result = registry.refresh()
    assert result["indexes"][0]["status"] == "error"
    # Bundled specs still serve.
    assert registry.get("vllm").version == "0.1.0"


def test_refresh_caches_to_disk_and_reload_uses_it(monkeypatch, tmp_path, index_file):
    monkeypatch.setattr("spark_pulse.engines.registry._is_simulation", lambda: False)
    monkeypatch.setattr(
        type(_config()), "engine_indexes", property(lambda self: [str(index_file)])
    )
    cache_dir = tmp_path / "cache"
    registry = EngineRegistry(cache_dir=cache_dir)
    registry.refresh()
    assert list(cache_dir.glob("*.json"))

    # A brand new registry picks the index up from disk without any fetch.
    def boom(ref):
        raise AssertionError("must not fetch")

    monkeypatch.setattr("spark_pulse.engines.registry.fetch_index", boom)
    fresh = EngineRegistry(cache_dir=cache_dir)
    assert fresh.get("vllm").version == "9.9.9"


def test_refresh_is_skipped_in_simulation_mode(monkeypatch, tmp_path):
    monkeypatch.setattr("spark_pulse.engines.registry._is_simulation", lambda: True)
    registry = EngineRegistry(cache_dir=tmp_path / "cache")
    result = registry.refresh()
    assert result["refreshed"] is False
    assert "simulation" in result["reason"]


# ── Selection ────────────────────────────────────────────────────────────────


def test_select_prefers_request_then_recipe_then_default(registry):
    assert registry.select("sglang", "vllm", "vllm") == ("sglang", "default")
    assert registry.select(None, "sglang", "vllm") == ("sglang", "default")
    assert registry.select(None, None, "vllm") == ("vllm", "default")
    assert registry.select(None, None, None) == ("vllm", "default")


def test_select_accepts_engine_slash_variant(registry):
    assert registry.select("vllm/default") == ("vllm", "default")


def test_select_rejects_unknown_engine(registry):
    with pytest.raises(EngineNotFound):
        registry.select("tensorrt")


def test_select_rejects_a_disabled_engine(monkeypatch, registry):
    monkeypatch.setattr(
        type(_config()),
        "engines",
        property(lambda self: {"sglang": {"enabled": False}}),
    )
    assert registry.enabled("sglang") is False
    with pytest.raises(Exception, match="disabled"):
        registry.select("sglang")


def _config():
    from spark_pulse.config import config

    return config


class TestAvailability:
    """An engine whose image was never published must not be offered.

    The index carries `available: false` for an image the publish workflow
    found no digest for. Pulling such a reference returns a 403 rather than an
    image, which a deployment discovers minutes later at pull time.
    """

    def test_an_index_entry_can_mark_itself_unavailable(self):
        from spark_pulse.engines.registry import parse_index

        image = "ghcr.io/example/spark-pulse-engine/vllm-b12x"
        entry = {
            "id": "vllm-b12x",
            "engine": "vllm",
            "variant": "b12x",
            "version": "0.1.0",
            "image": image,
            "digest": None,
            "available": False,
        }
        (spec,) = parse_index({"engines": [entry]}, "test-index")
        assert spec.available is False

    def test_availability_defaults_true_for_older_indexes(self):
        from spark_pulse.engines.registry import parse_index

        entry = {
            "id": "vllm",
            "engine": "vllm",
            "variant": "default",
            "version": "0.1.0",
            "image": "ghcr.io/example/vllm",
        }
        (spec,) = parse_index({"engines": [entry]}, "test-index")
        assert spec.available is True

    def test_usable_requires_both_available_and_enabled(self, registry):
        spec = registry.get("vllm")
        assert registry.usable(spec) is True
        spec.available = False
        assert registry.usable(spec) is False
