"""The recipes shipped inside the package.

Three things are checked: every bundled file validates against the published
v2 schema, discovery lists them as their own source, and every one of them
renders on both engines it declares. The last is the point of the whole
exercise — a recipe that only renders on vLLM would leave SGLang unusable.
"""

from pathlib import Path

import pytest
import yaml

from spark_pulse.engines import EngineRegistry, Topology
from spark_pulse.tools import recipe_schema, recipe_sources

# `recipes` is the mock under SIMULATION_MODE=1, which is what the API serves
# in simulation; discovery is shared with the real tools via recipe_sources.
from spark_pulse.tools import recipes

BUNDLED_IDS = [
    "bundled/qwen2.5-0.5b-instruct",
    "bundled/qwen3.5-35b-a3b-fp8",
    "bundled/qwen3.8-27b",
]

NO_CHECKOUT = Path("/nonexistent-spark-vllm-checkout")


def bundled_files() -> list[Path]:
    return recipe_sources.iter_bundled_recipe_files()


@pytest.fixture
def registry(tmp_path):
    """Bundled engine specs only — the tests stay offline."""
    return EngineRegistry(cache_dir=tmp_path / "engine-cache")


# ── Schema ───────────────────────────────────────────────────────────────────


def test_at_least_three_bundled_recipes_are_shipped():
    assert len(bundled_files()) >= 3


@pytest.mark.parametrize("path", bundled_files(), ids=lambda p: p.name)
def test_every_bundled_recipe_validates_strictly(path):
    result = recipe_schema.validate_recipe_file(path)
    assert result["ok"], result["errors"]
    assert result["recipe_version"] == "2"


@pytest.mark.parametrize("path", bundled_files(), ids=lambda p: p.name)
def test_every_bundled_recipe_declares_both_engines(path):
    recipe = recipe_schema.parse_recipe(path, strict=True)
    assert set(recipe.engines) == {"vllm", "sglang"}
    # Engine-specific flags belong under engines.<name>.args, never in params.
    for name in ("vllm", "sglang"):
        assert recipe.engines[name].args_string().strip()


@pytest.mark.parametrize("path", bundled_files(), ids=lambda p: p.name)
def test_every_bundled_recipe_is_engine_neutral_in_params(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "command" not in data, "a command template would pin the recipe"
    # Only keys the neutral param model knows; anything else is engine specific.
    known = set(recipe_schema.RecipeParams.model_fields)
    assert set(data["params"]) <= known


def test_the_smoke_recipe_is_cheap():
    """The 0.5B recipe is the one run on hardware: short context, small share."""
    recipe = recipe_schema.parse_recipe(
        recipe_sources.bundled_recipes_dir() / "qwen2.5-0.5b-instruct.yaml"
    )
    assert recipe.model == "Qwen/Qwen2.5-0.5B-Instruct"
    assert recipe.params.max_model_len == 8192
    assert recipe.params.gpu_memory_utilization == 0.2
    assert recipe.params.tensor_parallel == 1


# ── Discovery and listing ────────────────────────────────────────────────────


def test_candidate_files_lists_bundled_recipes_without_a_checkout():
    ids = [rid for rid, _ in recipe_sources.candidate_files(NO_CHECKOUT)]
    assert ids == BUNDLED_IDS


def test_list_recipes_labels_the_bundled_source():
    listed = {r["id"]: r for r in recipes.list_recipes(spark_path=NO_CHECKOUT)}
    for recipe_id in BUNDLED_IDS:
        assert listed[recipe_id]["source"] == "bundled"
        assert listed[recipe_id]["recipe_version"] == "2"
        assert listed[recipe_id]["engines"] == ["vllm", "sglang"]


def test_bundled_recipes_do_not_shadow_a_checkout_recipe(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()
    (recipe_dir / "local.yaml").write_text(
        "name: Local\nmodel: org/local\ncontainer: vllm-node\ncommand: vllm serve\n",
        encoding="utf-8",
    )
    listed = {r["id"]: r for r in recipes.list_recipes(spark_path=tmp_path)}
    assert set(BUNDLED_IDS) < set(listed)
    assert listed["local"]["source"] == "upstream"


@pytest.mark.parametrize("recipe_id", BUNDLED_IDS)
def test_get_recipe_resolves_a_bundled_id(recipe_id):
    payload = recipes.get_recipe(recipe_id, spark_path=NO_CHECKOUT)
    assert payload is not None
    assert payload["id"] == recipe_id
    assert payload["source"] == "bundled"
    assert set(payload["engine_specs"]) == {"vllm", "sglang"}


@pytest.mark.parametrize(
    "recipe_id,expected",
    [
        ("bundled/qwen2.5-0.5b-instruct", "bundled"),
        ("imported/cluster/big", "imported"),
        ("custom-mine", "custom"),
        ("oci-thing", "oci"),
        ("qwen3.5-35b", "upstream"),
    ],
)
def test_source_of(recipe_id, expected):
    assert recipe_sources.source_of(recipe_id) == expected


# ── Rendering on both engines ────────────────────────────────────────────────


@pytest.mark.parametrize("recipe_id", BUNDLED_IDS)
@pytest.mark.parametrize("engine_name", ["vllm", "sglang"])
def test_every_bundled_recipe_renders_on_both_engines(registry, recipe_id, engine_name):
    payload = recipes.get_recipe(recipe_id, spark_path=NO_CHECKOUT)
    engine = registry.engine(engine_name)

    supported, reason = engine.supports(payload)
    assert supported, reason

    result = engine.render(payload, topology=Topology.solo())
    assert payload["model"] in result.command
    # The engine's own args tail, not the other engine's.
    assert payload["engine_specs"][engine_name]["args"].split()[0] in result.command
    other = "sglang" if engine_name == "vllm" else "vllm"
    other_first = payload["engine_specs"][other]["args"].split()[0]
    if other_first not in payload["engine_specs"][engine_name]["args"]:
        assert other_first not in result.command


@pytest.mark.parametrize("recipe_id", BUNDLED_IDS)
def test_engine_support_reports_both_engines_usable(recipe_id):
    payload = recipes.get_recipe(recipe_id, spark_path=NO_CHECKOUT)
    support = {e["engine"]: e for e in payload["engine_support"]}
    assert set(support) == {"sglang", "vllm"}
    assert all(e["supported"] for e in support.values())
    assert all(e["reason"] == "" for e in support.values())


def test_a_v1_recipe_reports_sglang_as_unsupported_with_a_reason(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()
    (recipe_dir / "v1.yaml").write_text(
        "name: V1\nmodel: org/v1\ncontainer: vllm-node\ncommand: vllm serve org/v1\n",
        encoding="utf-8",
    )
    payload = recipes.get_recipe("v1", spark_path=tmp_path)
    support = {e["engine"]: e for e in payload["engine_support"]}
    assert support["vllm"]["supported"] is True
    assert support["sglang"]["supported"] is False
    assert "engine-specific command" in support["sglang"]["reason"]


def test_the_smoke_recipe_renders_the_hardware_command(registry):
    """The exact solo SGLang line that gets run on a GB10."""
    payload = recipes.get_recipe(
        "bundled/qwen2.5-0.5b-instruct", spark_path=NO_CHECKOUT
    )
    result = registry.engine("sglang").render(payload, topology=Topology.solo())
    assert result.command == (
        "python3 -m sglang.launch_server"
        " --model-path Qwen/Qwen2.5-0.5B-Instruct"
        " --tp 1 --pp-size 1 --host 0.0.0.0 --port 30000"
        " --mem-fraction-static 0.2 --context-length 8192"
        " --max-running-requests 16"
        " --nnodes 1 --node-rank 0 --dist-init-addr 127.0.0.1:50000"
        " --chunked-prefill-size 2048"
    )
    assert "--enable-dp-attention" not in result.command
    assert result.env["TIKTOKEN_ENCODINGS_BASE"] == "/root/tiktoken_encodings"
