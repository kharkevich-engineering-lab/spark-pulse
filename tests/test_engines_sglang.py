"""Golden-string tests for the SGLang engine renderer."""

import pytest

from spark_pulse.engines import EngineError, NodeInfo, SglangEngine, Topology
from spark_pulse.engines.registry import load_bundled_specs

RECIPE = {
    "id": "llama-8b",
    "name": "Llama 3.1 8B",
    "model": "meta-llama/Llama-3.1-8B-Instruct",
    "recipe_version": "2",
    "params": {"port": 30000, "host": "0.0.0.0", "gpu_memory_utilization": 0.9},
}

SERVE = "python3 -m sglang.launch_server --model-path meta-llama/Llama-3.1-8B-Instruct"

TWO_NODES = Topology(
    nodes=[
        NodeInfo(host="spark-a", ip="10.0.0.1", eth_if="enp1s0f0np0", ib_if="mlx5_0"),
        NodeInfo(host="spark-b", ip="10.0.0.2", eth_if="enp1s0f0np0", ib_if="mlx5_0"),
    ]
)


@pytest.fixture
def engine():
    spec = next(s for s in load_bundled_specs() if s.engine == "sglang")
    return SglangEngine(spec)


def test_solo_still_passes_rendezvous_flags(engine):
    result = engine.render(RECIPE)
    assert result.command == (
        f"{SERVE} --tp 1 --pp-size 1 --host 0.0.0.0 --port 30000"
        " --mem-fraction-static 0.9"
        " --nnodes 1 --node-rank 0 --dist-init-addr 127.0.0.1:50000"
    )


def test_solo_omits_multi_node_extra_args(engine):
    assert "--enable-dp-attention" not in engine.render(RECIPE).command


def test_two_node_rank0(engine):
    result = engine.render(
        RECIPE, params={"tensor_parallel": 2}, topology=TWO_NODES, node_rank=0
    )
    assert result.command == (
        f"{SERVE} --tp 2 --pp-size 1 --host 0.0.0.0 --port 30000"
        " --mem-fraction-static 0.9"
        " --nnodes 2 --node-rank 0 --dist-init-addr 10.0.0.1:50000"
        " --enable-dp-attention"
    )
    assert result.host == "spark-a"


def test_two_node_rank1(engine):
    result = engine.render(
        RECIPE, params={"tensor_parallel": 2}, topology=TWO_NODES, node_rank=1
    )
    assert result.command == (
        f"{SERVE} --tp 2 --pp-size 1 --host 0.0.0.0 --port 30000"
        " --mem-fraction-static 0.9"
        " --nnodes 2 --node-rank 1 --dist-init-addr 10.0.0.1:50000"
        " --enable-dp-attention"
    )
    assert result.host == "spark-b"


def test_context_length_from_max_model_len(engine):
    result = engine.render(RECIPE, params={"max_model_len": 262144})
    assert "--context-length 262144" in result.command


def test_extra_args_are_shell_quoted(engine):
    result = engine.render(RECIPE, extra_args=["--chat-template", "my template.jinja"])
    assert result.command.endswith("--chat-template 'my template.jinja'")


def test_engine_specific_args_from_v2_recipe(engine):
    recipe = {**RECIPE, "engines": {"sglang": {"args": "--tool-call-parser qwen25"}}}
    result = engine.render(recipe)
    assert result.command.endswith(
        "--dist-init-addr 127.0.0.1:50000 --tool-call-parser qwen25"
    )


def test_model_override(engine):
    result = engine.render(RECIPE, model="Qwen/Qwen3-8B")
    assert result.command.startswith(
        "python3 -m sglang.launch_server --model-path Qwen/Qwen3-8B "
    )


def test_supports_rejects_v1_recipes_with_a_vllm_command(engine):
    ok, reason = engine.supports({"id": "x", "command": "vllm serve M --port 8000"})
    assert ok is False
    assert "engine-specific command" in reason


def test_render_refuses_v1_recipe(engine):
    with pytest.raises(EngineError, match="engine-specific command"):
        engine.render({"id": "x", "model": "m", "command": "vllm serve M"})


def test_base_env_and_script(engine):
    result = engine.render(RECIPE, topology=TWO_NODES, node_rank=0)
    assert result.env["HF_HOME"] == "/root/.cache/huggingface"
    assert result.env["NCCL_IB_DISABLE"] == "0"
    assert result.env["NCCL_SOCKET_IFNAME"] == "enp1s0f0np0"
    assert result.env["GLOO_SOCKET_IFNAME"] == "enp1s0f0np0"
    assert result.env["NCCL_IB_HCA"] == "mlx5_0"
    assert 'export HF_HOME="/root/.cache/huggingface"' in result.script


def test_declarative_accessors(engine):
    assert engine.readiness_path() == "/health"
    assert engine.api_port() == 30000
    assert engine.rendezvous_port() == 50000
    assert engine.supports_mods() is False
    profile = engine.container_profile()
    assert profile["privileged"] is False
    assert profile["shm_size_gb"] == 32
    assert profile["devices"] == ["/dev/infiniband"]
    assert engine.default_image().endswith("/sglang:0.1.0")
