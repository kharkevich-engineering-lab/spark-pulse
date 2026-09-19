import json
from pathlib import Path

from spark_pulse.tools import custom_files, recipes

# `recipes` above is the mock under SIMULATION_MODE=1, which is what the API
# serves in simulation. Discovery/parsing/rendering is shared with the real
# tools through recipe_sources (no mock twin), so tests that are about that
# logic address it directly.
from spark_pulse.tools import recipe_sources


def without_bundled(entries: list[dict]) -> list[dict]:
    """Drop the recipes shipped inside the package.

    Bundled recipes are always listed, so tests about the operator's own
    directory filter them out rather than pretending they are absent.
    """
    return [e for e in entries if e.get("source") != recipe_sources.SOURCE_BUNDLED]


def custom_dir() -> Path:
    """The operator's own recipe directory, redirected to tmp_path by conftest.

    It is the only place a recipe that is not bundled or OCI-installed can
    live: a recipe id is ``custom-<stem>``, which is the id the removed
    checkout symlinks minted.
    """
    directory = custom_files.custom_recipes_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_list_recipes_parses_valid_and_skips_bad_yaml():
    recipe_dir = custom_dir()

    (recipe_dir / "valid.yaml").write_text(
        """
name: TinyLlama
model: TinyLlama/TinyLlama-1.1B
container: vllm-node
defaults:
  port: 8123
""".strip(),
        encoding="utf-8",
    )
    (recipe_dir / "broken.yaml").write_text("name: [", encoding="utf-8")

    out = without_bundled(recipes.list_recipes())

    assert len(out) == 1
    assert out[0]["id"] == "custom-valid"
    assert out[0]["name"] == "TinyLlama"
    assert out[0]["defaults"]["port"] == 8123


def test_get_recipe_returns_none_for_missing():
    assert recipes.get_recipe("does-not-exist") is None


def test_get_recipe_returns_recipe_payload():
    (custom_dir() / "qwen.yaml").write_text(
        """
name: Qwen
model: Qwen/Qwen2.5
command: vllm serve {model} --port {port} {-tp}
defaults:
  port: 9001
""".strip(),
        encoding="utf-8",
    )

    out = recipes.get_recipe("custom-qwen")

    assert out is not None
    assert out["id"] == "custom-qwen"
    assert out["name"] == "Qwen"
    assert out["command"].startswith("vllm serve")
    assert out["defaults"]["port"] == 9001


def test_get_recipe_resolves_a_display_name():
    """A recipe is addressable by the name it declares, not only by its id."""
    (custom_dir() / "qwen.yaml").write_text(
        "name: Qwen\nmodel: Qwen/Qwen2.5\ncommand: vllm serve\n",
        encoding="utf-8",
    )

    out = recipes.get_recipe("Qwen")

    assert out is not None
    assert out["id"] == "custom-qwen"


def test_a_subdirectory_is_not_a_recipe_source():
    """The custom directory is flat, and always was for the ids it mints.

    Nested ids (``cluster/big-model``) only ever came from the ``recipes/``
    tree of a spark-vllm-docker checkout, which is no longer read.
    """
    nested = custom_dir() / "cluster"
    nested.mkdir()
    (nested / "big-model.yaml").write_text(
        "name: Big Model (PP=3)\nmodel: vendor/big\n", encoding="utf-8"
    )

    assert without_bundled(recipes.list_recipes()) == []
    assert recipes.get_recipe("cluster/big-model") is None


def test_build_launch_command_replaces_supported_tokens():
    recipe = {
        "command": "vllm serve --host {host} --port {port} {-tp} --gpu-memory-utilization {--gpu-memory-utilization} --max-model-len {--max-model-len}"
    }
    params = {
        "host": "127.0.0.1",
        "port": 9100,
        "tensor_parallel": 4,
        "gpu_memory_utilization": 0.92,
        "max_model_len": 4096,
    }

    cmd = recipes.build_launch_command(recipe, params)

    assert "--host 127.0.0.1" in cmd
    assert "--port 9100" in cmd
    assert "--tensor-parallel-size 4" in cmd
    assert "--gpu-memory-utilization 0.92" in cmd
    assert "--max-model-len 4096" in cmd


def test_get_recipe_applies_saved_customization(tmp_path, monkeypatch):
    (custom_dir() / "qwen.yaml").write_text(
        """
name: Qwen
model: Qwen/Qwen2.5
container: vllm-node
command: vllm serve {model} --port {port}
defaults:
  port: 9001
""".strip(),
        encoding="utf-8",
    )

    custom_path = tmp_path / "custom-recipes.json"
    custom_path.write_text(
        json.dumps(
            {
                "custom-qwen": {
                    "command": "custom serve {model}",
                    "defaults": {"port": 9010},
                    "mods": ["my-mod"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(recipes.custom_recipes, "_CUSTOM_PATH", custom_path)

    out = recipes.get_recipe("custom-qwen")

    assert out is not None
    assert out["command"] == "custom serve {model}"
    assert out["defaults"]["port"] == 9010
    assert out["mods"] == ["my-mod"]


V2_RECIPE = """
recipe_version: "2"
name: Structured
model: org/structured
description: A v2 recipe.
engine: vllm
constraints:
  cluster_only: true
  min_nodes: 2
params:
  port: 9200
  tensor_parallel: 2
engines:
  vllm:
    image: vllm-node-b12x
    mods: [fix-something]
    env: {VLLM_USE_V1: "1"}
    args: --enable-prefix-caching
  sglang:
    args: --mem-fraction-static 0.85
""".strip()


def test_list_recipes_reports_schema_fields_for_v1():
    (custom_dir() / "tiny.yaml").write_text(
        "name: Tiny\nmodel: org/tiny\ncontainer: vllm-node\ncommand: vllm serve\n",
        encoding="utf-8",
    )

    out = without_bundled(recipes.list_recipes())[0]

    assert out["recipe_version"] == "1"
    assert out["engine"] is None
    assert out["engines"] == ["vllm"]
    assert out["params"] == out["defaults"]


def test_list_recipes_reports_schema_fields_for_v2():
    (custom_dir() / "structured.yaml").write_text(V2_RECIPE, encoding="utf-8")

    out = without_bundled(recipes.list_recipes())[0]

    assert out["recipe_version"] == "2"
    assert out["engine"] == "vllm"
    assert out["engines"] == ["vllm", "sglang"]
    assert out["params"] == {"port": 9200, "tensor_parallel": 2}
    assert out["defaults"] == out["params"]
    assert out["container"] == "vllm-node-b12x"
    assert out["cluster_only"] is True
    assert out["mods"] == ["fix-something"]


def test_get_recipe_returns_v2_detail():
    (custom_dir() / "structured.yaml").write_text(V2_RECIPE, encoding="utf-8")

    out = recipes.get_recipe("custom-structured")

    assert out is not None
    assert out["recipe_version"] == "2"
    assert out["model"] == "org/structured"
    assert out["env"] == {"VLLM_USE_V1": "1"}
    assert out["min_nodes"] == 2
    assert out["build_args"] == []


def test_render_command_supports_plain_placeholders():
    recipe = {
        "command": (
            "vllm serve --port {port} -tp {tensor_parallel} "
            "--gpu-memory-utilization {gpu_memory_utilization} "
            "--max-model-len {max_model_len}"
        )
    }
    cmd = recipe_sources.render_command(
        recipe,
        {
            "port": 9100,
            "tensor_parallel": 4,
            "gpu_memory_utilization": 0.9,
            "max_model_len": 2048,
        },
    )

    assert cmd == (
        "vllm serve --port 9100 -tp 4 --gpu-memory-utilization 0.9 --max-model-len 2048"
    )


def test_render_command_warns_on_deprecated_placeholders(caplog):
    recipe = {"id": "legacy", "command": "vllm serve {-tp}"}

    with caplog.at_level("WARNING"):
        cmd = recipe_sources.render_command(recipe, {"tensor_parallel": 2})

    assert "--tensor-parallel-size 2" in cmd
    assert "deprecated" in caplog.text
    assert "legacy" in caplog.text


def test_render_command_does_not_warn_for_modern_recipes(caplog):
    recipe = {"id": "modern", "command": "vllm serve -tp {tensor_parallel}"}

    with caplog.at_level("WARNING"):
        recipe_sources.render_command(recipe, {"tensor_parallel": 2})

    assert "deprecated" not in caplog.text
