"""Golden-string tests for the vLLM engine renderer."""

import pytest

from spark_pulse.engines import EngineError, NodeInfo, Topology, VllmEngine
from spark_pulse.engines.registry import load_bundled_specs

V1_COMMAND = """
vllm serve Qwen/Qwen3-8B \\
  --port {port} \\
  --tensor-parallel-size {tensor_parallel} \\
  --gpu-memory-utilization {gpu_memory_utilization} \\
  --distributed-executor-backend ray
""".strip()

V1_RECIPE = {
    "id": "qwen3-8b",
    "name": "Qwen3 8B",
    "model": "Qwen/Qwen3-8B",
    "command": V1_COMMAND,
    "defaults": {"port": 8000, "tensor_parallel": 2, "gpu_memory_utilization": 0.8},
}

BASE = (
    "vllm serve Qwen/Qwen3-8B --port 8000 "
    "--tensor-parallel-size {tp} --gpu-memory-utilization 0.8"
)

TWO_NODES = Topology(
    nodes=[
        NodeInfo(host="spark-a", ip="10.0.0.1", eth_if="enp1s0f0np0", ib_if="mlx5_0"),
        NodeInfo(host="spark-b", ip="10.0.0.2", eth_if="enp1s0f0np0", ib_if="mlx5_0"),
    ]
)


@pytest.fixture
def engine():
    spec = next(s for s in load_bundled_specs() if s.engine == "vllm")
    return VllmEngine(spec)


def test_solo_forces_tp1_and_strips_executor_backend(engine):
    result = engine.render(V1_RECIPE)
    assert result.command == BASE.format(tp=1)
    assert "--distributed-executor-backend" not in result.command
    assert result.node_rank == 0


def test_solo_keeps_explicit_tensor_parallel_override(engine):
    result = engine.render(V1_RECIPE, params={"tensor_parallel": 4})
    assert result.command == BASE.format(tp=4)


def test_two_node_rank0_appends_rendezvous_args(engine):
    result = engine.render(V1_RECIPE, topology=TWO_NODES, node_rank=0)
    assert result.command == (
        BASE.format(tp=2)
        + " --nnodes 2 --node-rank 0 --master-addr 10.0.0.1 --master-port 29501"
    )
    assert result.host == "spark-a"


def test_two_node_rank1_is_headless(engine):
    result = engine.render(V1_RECIPE, topology=TWO_NODES, node_rank=1)
    assert result.command == (
        BASE.format(tp=2)
        + " --nnodes 2 --node-rank 1 --master-addr 10.0.0.1 --master-port 29501"
        " --headless"
    )
    assert result.host == "spark-b"


def test_rank_out_of_range(engine):
    with pytest.raises(EngineError, match="out of range"):
        engine.render(V1_RECIPE, topology=TWO_NODES, node_rank=2)


def test_extra_args_are_shell_quoted(engine):
    result = engine.render(
        V1_RECIPE, extra_args=["--chat-template", "my template.jinja", "--trust"]
    )
    assert result.command.endswith("--chat-template 'my template.jinja' --trust")


def test_double_braces_stay_literal(engine):
    recipe = {
        **V1_RECIPE,
        "command": "vllm serve M --port {port} --override '{{\"a\": 1}}'",
    }
    result = engine.render(recipe)
    assert result.command == "vllm serve M --port 8000 --override '{\"a\": 1}'"


def test_missing_placeholder_raises_clear_error(engine):
    recipe = {**V1_RECIPE, "command": "vllm serve M --port {port} --hey {nope}"}
    with pytest.raises(EngineError) as exc:
        engine.render(recipe)
    assert "{nope}" in str(exc.value)
    assert "available params" in str(exc.value)


def test_base_env_and_script(engine):
    result = engine.render(V1_RECIPE, topology=TWO_NODES, node_rank=0)
    assert result.env["VLLM_HOST_IP"] == "10.0.0.1"
    assert result.env["MN_IF_NAME"] == "enp1s0f0np0"
    assert result.env["UCX_NET_DEVICES"] == "enp1s0f0np0"
    assert result.env["NCCL_SOCKET_IFNAME"] == "enp1s0f0np0"
    assert result.env["NCCL_IB_HCA"] == "mlx5_0"
    assert result.env["NCCL_IB_DISABLE"] == "0"
    assert result.env["OMPI_MCA_btl_tcp_if_include"] == "enp1s0f0np0"
    assert result.env["GLOO_SOCKET_IFNAME"] == "enp1s0f0np0"
    assert result.env["TP_SOCKET_IFNAME"] == "enp1s0f0np0"
    # Spec runtime env is merged in.
    assert result.env["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
    assert 'export VLLM_HOST_IP="10.0.0.1"' in result.script
    assert result.script.startswith("#!/usr/bin/env bash")


def test_recipe_env_overrides_base_env(engine):
    recipe = {**V1_RECIPE, "env": {"NCCL_IB_DISABLE": "1"}}
    result = engine.render(recipe)
    assert result.env["NCCL_IB_DISABLE"] == "1"


def test_v2_recipe_without_command(engine):
    recipe = {
        "id": "generic",
        "model": "Qwen/Qwen3-8B",
        "recipe_version": "2",
        "params": {"host": "0.0.0.0", "port": 9000, "max_model_len": 4096},
        "engines": {"vllm": {"args": "--enable-prefix-caching"}},
    }
    result = engine.render(recipe)
    assert result.command == (
        "vllm serve Qwen/Qwen3-8B --host 0.0.0.0 --port 9000 "
        "--tensor-parallel-size 1 --max-model-len 4096 --enable-prefix-caching"
    )


def test_v2_recipe_model_override(engine):
    recipe = {"id": "generic", "model": "a/b", "params": {"port": 8000}}
    result = engine.render(recipe, model="c/d")
    assert result.command.startswith("vllm serve c/d ")


def test_v2_recipe_without_model_raises(engine):
    with pytest.raises(EngineError, match="no model"):
        engine.render({"id": "generic", "params": {"port": 8000}})


def test_supports_rejects_recipe_pinned_to_another_engine(engine):
    ok, reason = engine.supports({"id": "x", "engine": "sglang"})
    assert ok is False
    assert "sglang" in reason


def test_supports_rejects_recipe_declaring_only_other_engines(engine):
    ok, reason = engine.supports({"id": "x", "engines": {"sglang": {}}})
    assert ok is False
    assert "sglang" in reason


def test_declarative_accessors(engine):
    assert engine.readiness_path() == "/v1/models"
    assert engine.metrics_path() == "/metrics"
    assert engine.api_port() == 8000
    assert engine.rendezvous_port() == 29501
    assert engine.supports_mods() is True
    assert "~/.cache/vllm" in engine.cache_mounts()
    profile = engine.container_profile()
    assert profile["privileged"] is True
    assert profile["network_host"] is True
    assert profile["ulimits"] == {"nofile": "1048576:1048576"}
    assert engine.default_image().endswith("/vllm:0.1.0")
