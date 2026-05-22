from spark_pulse.tools import recipes


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
