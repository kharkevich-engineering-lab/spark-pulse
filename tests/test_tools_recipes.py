import json
import sys

from spark_pulse.tools import recipes

# `recipes` above is the mock under SIMULATION_MODE=1, which is what the API
# serves in simulation. Discovery/parsing/rendering is shared with the real
# tools through recipe_sources (no mock twin), so tests that are about that
# logic address it directly.
from spark_pulse.tools import recipe_sources


def test_list_recipes_parses_valid_and_skips_bad_yaml(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()

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

    out = recipes.list_recipes(spark_path=tmp_path)

    assert len(out) == 1
    assert out[0]["id"] == "valid"
    assert out[0]["name"] == "TinyLlama"
    assert out[0]["defaults"]["port"] == 8123


def test_get_recipe_returns_none_for_missing(tmp_path):
    assert recipes.get_recipe("does-not-exist", spark_path=tmp_path) is None


def test_get_recipe_returns_recipe_payload(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()
    (recipe_dir / "qwen.yaml").write_text(
        """
name: Qwen
model: Qwen/Qwen2.5
command: vllm serve {model} --port {port} {-tp}
defaults:
  port: 9001
""".strip(),
        encoding="utf-8",
    )

    out = recipes.get_recipe("qwen", spark_path=tmp_path)

    assert out is not None
    assert out["id"] == "qwen"
    assert out["name"] == "Qwen"
    assert out["command"].startswith("vllm serve")
    assert out["defaults"]["port"] == 9001


def test_list_recipes_scans_subdirectories(tmp_path):
    recipe_dir = tmp_path / "recipes"
    (recipe_dir / "cluster").mkdir(parents=True)
    (recipe_dir / "cluster" / "big-model.yaml").write_text(
        "name: Big Model (PP=3)\nmodel: vendor/big\n", encoding="utf-8"
    )
    (recipe_dir / "small.yaml").write_text(
        "name: Small Model\nmodel: vendor/small\n", encoding="utf-8"
    )

    out = recipes.list_recipes(spark_path=tmp_path)
    ids = [r["id"] for r in out]

    assert "small" in ids
    assert "cluster/big-model" in ids


def test_get_recipe_finds_subdirectory_recipe(tmp_path):
    recipe_dir = tmp_path / "recipes"
    (recipe_dir / "cluster").mkdir(parents=True)
    (recipe_dir / "cluster" / "big-model.yaml").write_text(
        "name: Big Model (PP=3)\nmodel: vendor/big\ncommand: vllm serve\n",
        encoding="utf-8",
    )

    out = recipes.get_recipe("cluster/big-model", spark_path=tmp_path)

    assert out is not None
    assert out["id"] == "cluster/big-model"
    assert out["name"] == "Big Model (PP=3)"


def test_list_recipes_includes_extensionless_symlink(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()

    custom_file = tmp_path / "custom-new.yaml"
    custom_file.write_text("name: Custom New\nmodel: vendor/custom\n", encoding="utf-8")
    (recipe_dir / "custom-new").symlink_to(custom_file)

    out = recipes.list_recipes(spark_path=tmp_path)
    ids = [r["id"] for r in out]

    assert "custom-new" in ids


def test_get_recipe_reads_extensionless_symlink(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()

    custom_file = tmp_path / "custom-uploaded.yaml"
    custom_file.write_text(
        "name: Uploaded Recipe\nmodel: vendor/uploaded\n", encoding="utf-8"
    )
    (recipe_dir / "custom-uploaded").symlink_to(custom_file)

    out = recipes.get_recipe("custom-uploaded", spark_path=tmp_path)

    assert out is not None
    assert out["id"] == "custom-uploaded"
    assert out["name"] == "Uploaded Recipe"


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
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()
    (recipe_dir / "qwen.yaml").write_text(
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
                "qwen": {
                    "command": "custom serve {model}",
                    "defaults": {"port": 9010},
                    "mods": ["my-mod"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(recipes.custom_recipes, "_CUSTOM_PATH", custom_path)

    out = recipes.get_recipe("qwen", spark_path=tmp_path)

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


def test_list_recipes_reports_schema_fields_for_v1(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()
    (recipe_dir / "tiny.yaml").write_text(
        "name: Tiny\nmodel: org/tiny\ncontainer: vllm-node\ncommand: vllm serve\n",
        encoding="utf-8",
    )

    out = recipes.list_recipes(spark_path=tmp_path)[0]

    assert out["recipe_version"] == "1"
    assert out["engine"] is None
    assert out["engines"] == ["vllm"]
    assert out["params"] == out["defaults"]


def test_list_recipes_reports_schema_fields_for_v2(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()
    (recipe_dir / "structured.yaml").write_text(V2_RECIPE, encoding="utf-8")

    out = recipes.list_recipes(spark_path=tmp_path)[0]

    assert out["recipe_version"] == "2"
    assert out["engine"] == "vllm"
    assert out["engines"] == ["vllm", "sglang"]
    assert out["params"] == {"port": 9200, "tensor_parallel": 2}
    assert out["defaults"] == out["params"]
    assert out["container"] == "vllm-node-b12x"
    assert out["cluster_only"] is True
    assert out["mods"] == ["fix-something"]


def test_get_recipe_returns_v2_detail(tmp_path):
    recipe_dir = tmp_path / "recipes"
    recipe_dir.mkdir()
    (recipe_dir / "structured.yaml").write_text(V2_RECIPE, encoding="utf-8")

    out = recipes.get_recipe("structured", spark_path=tmp_path)

    assert out is not None
    assert out["recipe_version"] == "2"
    assert out["model"] == "org/structured"
    assert out["env"] == {"VLLM_USE_V1": "1"}
    assert out["min_nodes"] == 2
    assert out["build_args"] == []


def test_list_recipes_includes_imported_source(tmp_path, monkeypatch):
    recipe_import = sys.modules["spark_pulse.tools.recipe_import"]

    imported = tmp_path / "imported"
    (imported / "recipes" / "cluster").mkdir(parents=True)
    (imported / "recipes" / "cluster" / "big.yaml").write_text(
        "name: Big\nmodel: org/big\ncontainer: vllm-node\ncommand: vllm serve\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recipe_import, "IMPORTED_DIR", imported)

    (tmp_path / "recipes").mkdir()
    (tmp_path / "recipes" / "local.yaml").write_text(
        "name: Local\nmodel: org/local\n", encoding="utf-8"
    )

    ids = [r["id"] for r in recipes.list_recipes(spark_path=tmp_path)]
    assert ids == ["local", "imported/cluster/big"]


def test_get_recipe_resolves_an_imported_id(tmp_path, monkeypatch):
    recipe_import = sys.modules["spark_pulse.tools.recipe_import"]

    imported = tmp_path / "imported"
    (imported / "recipes").mkdir(parents=True)
    (imported / "recipes" / "tiny.yaml").write_text(
        "name: Imported Tiny\nmodel: org/tiny\ncontainer: vllm-node\ncommand: x\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recipe_import, "IMPORTED_DIR", imported)

    out = recipes.get_recipe("imported/tiny", spark_path=tmp_path)

    assert out is not None
    assert out["id"] == "imported/tiny"
    assert out["name"] == "Imported Tiny"


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
