"""An engine the index adds runs without a control-plane release.

Two halves: the registry picks a renderer for a spec whose engine name this
build has never heard of, and a recipe may pin a variant with the
``engine: <engine>/<variant>`` form the registry already takes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from spark_pulse.engines import EngineError, EngineNotFound, Topology
from spark_pulse.engines.base import EngineSpec, NodeInfo
from spark_pulse.engines.registry import (
    EngineRegistry,
    engine_class,
)
from spark_pulse.engines.solo import GenericEngine
from spark_pulse.engines.vllm import VllmEngine
from spark_pulse.tools import recipe_schema


def _spec(**overrides) -> EngineSpec:
    data = {
        "engine": "newcomer",
        "variant": "default",
        "version": "0.1.0",
        "image": "ghcr.io/example/newcomer",
        "runtime": {
            "serve": "newcomer serve",
            "model_arg": "--model",
            "ports": {"api": 9000},
            "param_flags": {"port": "--port", "host": "--host"},
        },
        "capabilities": {"solo": True, "cluster": True},
    }
    for key, value in overrides.items():
        if key == "runtime":
            data["runtime"] = {**data["runtime"], **value}
        else:
            data[key] = value
    return EngineSpec.model_validate(data)


RECIPE = {"name": "r", "model": "org/model", "engine": "newcomer"}


def test_an_unknown_engine_renders_from_its_spec():
    engine = engine_class(_spec())(_spec())
    assert isinstance(engine, GenericEngine)
    assert engine.name == "newcomer"
    launch = engine.render(RECIPE)
    assert launch.command == (
        "newcomer serve --model org/model --host 0.0.0.0 --port 9000"
    )


def test_an_unknown_engine_answers_to_its_own_name_in_a_recipe():
    engine = GenericEngine(_spec())
    recipe = {
        "name": "r",
        "model": "org/model",
        "engines": {"newcomer": {"args": "--fast"}},
    }
    assert engine.supports(recipe) == (True, "")
    assert engine.render(recipe).command.endswith("--fast")
    assert not engine.supports({**recipe, "engines": {"vllm": {}}})[0]


def test_the_generic_renderer_refuses_two_nodes_whatever_the_spec_claims():
    engine = GenericEngine(_spec(runtime={"multi_node": {"style": "novel"}}))
    ok, reason = engine.supports_size(2)
    assert not ok
    assert "novel" in reason
    assert engine.supports_size(1) == (True, "")
    topology = Topology(nodes=[NodeInfo(host="a"), NodeInfo(host="b")])
    with pytest.raises(EngineError):
        engine.render(RECIPE, topology=topology)


def test_a_spec_names_the_renderer_it_speaks():
    spec = _spec(engine="vllm-fork", runtime={"renderer": "vllm"})
    engine = engine_class(spec)(spec)
    assert isinstance(engine, VllmEngine)
    assert engine.name == "vllm-fork"


def test_a_renderer_this_build_lacks_is_refused_by_name():
    spec = _spec(runtime={"renderer": "from-the-future"})
    with pytest.raises(EngineNotFound, match="from-the-future"):
        engine_class(spec)


def test_a_known_multi_node_style_picks_its_renderer():
    from spark_pulse.engines.llama_cpp import LlamaCppEngine

    spec = _spec(runtime={"multi_node": {"style": "llama-rpc"}})
    assert engine_class(spec) is LlamaCppEngine


def _registry(tmp_path: Path, *specs: dict) -> EngineRegistry:
    defaults = tmp_path / "defaults"
    defaults.mkdir()
    for index, spec in enumerate(specs):
        (defaults / f"{index}.yaml").write_text(yaml.safe_dump(spec))
    return EngineRegistry(specs_dir=defaults, cache_dir=tmp_path / "cache")


SGLANG = {"engine": "sglang", "variant": "default", "image": "x/sglang"}
INKLING = {"engine": "sglang", "variant": "inkling", "image": "x/sglang-inkling"}


def test_a_recipe_may_pin_a_variant():
    parsed = recipe_schema.parse_recipe(
        {
            "recipe_version": "2",
            "name": "r",
            "model": "org/model",
            "engine": "sglang/inkling",
            "engines": {"sglang": {"args": "--x"}},
        }
    )
    assert parsed.engine == "sglang"
    assert parsed.variant == "inkling"
    assert parsed.engine_names() == ["sglang"]


def test_the_pinned_variant_is_what_a_deploy_selects(tmp_path):
    registry = _registry(tmp_path, SGLANG, INKLING)
    recipe = {"engine": "sglang", "variant": "inkling"}
    assert registry.select_for_recipe(recipe) == ("sglang", "inkling")
    # Naming the same engine bare has not chosen another image.
    assert registry.select_for_recipe(recipe, "sglang") == ("sglang", "inkling")
    # Naming a variant has.
    assert registry.select_for_recipe(recipe, "sglang", "default") == (
        "sglang",
        "default",
    )
    assert registry.select_for_recipe(recipe, None, "default") == (
        "sglang",
        "default",
    )


def test_an_engine_with_no_default_variant_is_still_asked(tmp_path):
    registry = _registry(tmp_path, INKLING)
    assert registry.variant_for("sglang") == "inkling"
    assert registry.variant_for("sglang", "inkling") == "inkling"


def test_a_fork_runs_recipes_written_for_the_dialect_it_speaks():
    spec = _spec(
        engine="vllm-fork", runtime={"renderer": "vllm", "serve": "vllm serve"}
    )
    engine = engine_class(spec)(spec)
    assert engine.answers_to == ("vllm-fork", "vllm")
    assert engine.supports({"name": "r", "model": "m", "engines": {"vllm": {}}})[0]
    assert engine.supports({"name": "r", "model": "m", "command": "vllm serve m"})[0]
    # Its own block wins over the dialect's.
    recipe = {"engines": {"vllm": {"args": "--a"}, "vllm-fork": {"args": "--b"}}}
    assert engine._block_args(recipe) == "--b"


def test_the_engine_table_refuses_a_size_the_engine_cannot_render(
    tmp_path, monkeypatch
):
    from spark_pulse.engines import registry as registry_module
    from spark_pulse.tools import recipe_sources

    newcomer = {
        "engine": "newcomer",
        "image": "x/newcomer",
        "runtime": {"serve": "newcomer serve"},
        "capabilities": {"solo": True, "cluster": True},
    }
    reg = _registry(tmp_path, newcomer)
    monkeypatch.setattr(registry_module, "get_registry", lambda: reg)
    monkeypatch.setattr("spark_pulse.engines.get_registry", lambda: reg)
    payload = {"name": "r", "model": "m", "engine": "newcomer"}
    [row] = recipe_sources.engine_support(payload)
    assert row["supported"] is True
    [row] = recipe_sources.engine_support({**payload, "cluster_only": True})
    assert row["supported"] is False
    assert "one node" in row["reason"]


def test_a_failed_fetch_is_retried_after_the_backoff(tmp_path, monkeypatch, index_path):
    from spark_pulse.engines import registry as registry_module

    monkeypatch.setattr(registry_module, "_is_simulation", lambda: False)
    monkeypatch.setattr(
        EngineRegistry, "index_refs", property(lambda self: [str(index_path)])
    )
    calls = []

    def flaky(ref):
        calls.append(ref)
        if len(calls) == 1:
            raise RuntimeError("registry down")
        return yaml.safe_load(index_path.read_text())

    monkeypatch.setattr(registry_module, "fetch_index", flaky)
    clock = [1_000_000.0]
    monkeypatch.setattr(registry_module.time, "time", lambda: clock[0])
    reg = EngineRegistry(cache_dir=tmp_path / "cache", cache_ttl=3600)
    reg.refresh_in_background().join(timeout=10)
    assert reg.list() == []
    [status] = reg.index_status()
    assert status["error"] == "registry down" and status["engines"] == 0
    # Within the backoff nothing is started; after it, the fetch runs again.
    assert reg.refresh_in_background() is None
    clock[0] += registry_module.RETRY_SECONDS
    reg.refresh_in_background().join(timeout=10)
    assert [s.engine for s in reg.list()] == ["newcomer"]
    assert len(calls) == 2
    [status] = reg.index_status()
    assert status["error"] is None and status["engines"] == 1
    assert status["fetched_at"] == clock[0]
    # The cache was written whole, and nothing temporary was left beside it.
    assert [p.suffix for p in (tmp_path / "cache").iterdir()] == [".json"]


@pytest.fixture
def index_path(tmp_path):
    path = tmp_path / "index.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "kind": "EngineIndex",
                "engines": [
                    {
                        "engine": "newcomer",
                        "spec": {
                            "engine": "newcomer",
                            "image": "x/newcomer",
                            "runtime": {"serve": "newcomer serve"},
                        },
                    }
                ],
            }
        )
    )
    return path
