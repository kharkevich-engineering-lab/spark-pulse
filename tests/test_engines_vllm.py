"""Golden-string tests for the vLLM engine renderer."""

import socket

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


def _node(host: str, ip: str) -> NodeInfo:
    return NodeInfo(host=host, ip=ip, eth_if="enp1s0f0np0", ib_if="mlx5_0")


ONE_NODE = Topology(nodes=[_node("spark-a", "10.0.0.1")])
TWO_NODES = Topology(nodes=[_node("spark-a", "10.0.0.1"), _node("spark-b", "10.0.0.2")])
THREE_NODES = Topology(
    nodes=[
        _node("spark-a", "10.0.0.1"),
        _node("spark-b", "10.0.0.2"),
        _node("spark-c", "10.0.0.3"),
    ]
)


def rendezvous(nnodes: int, rank: int, addr: str = "10.0.0.1") -> str:
    """The rendezvous tail every render carries, at every size."""
    tail = f" --nnodes {nnodes} --node-rank {rank} --master-addr {addr} --master-port 29501"
    return tail + " --headless" if rank else tail


@pytest.fixture
def engine():
    spec = next(s for s in load_bundled_specs() if s.engine == "vllm")
    return VllmEngine(spec)


def test_one_node_renders_the_rendezvous_flags(engine):
    """No solo shape any more: one node renders like every other size."""
    result = engine.render(V1_RECIPE, topology=ONE_NODE)
    assert result.command == BASE.format(tp=2) + rendezvous(1, 0)
    assert result.node_rank == 0
    assert result.host == "spark-a"


def test_the_default_topology_is_this_machine(engine):
    """An absent topology means one node — this one — not zero nodes."""
    result = engine.render(V1_RECIPE)
    assert result.command == BASE.format(tp=2) + rendezvous(1, 0, addr="127.0.0.1")
    assert result.host == socket.gethostname()


def test_no_engine_size_ever_carries_the_executor_backend(engine):
    """The Ray backend flag is stripped at every size, not just solo."""
    for topology in (ONE_NODE, TWO_NODES, THREE_NODES):
        for rank in range(topology.size):
            rendered = engine.render(V1_RECIPE, topology=topology, node_rank=rank)
            assert "--distributed-executor-backend" not in rendered.command


def test_one_node_no_longer_rewrites_tensor_parallel(engine):
    """The old solo path forced tp=1; the recipe's own value now stands."""
    assert "--tensor-parallel-size 2" in engine.render(V1_RECIPE).command


def test_explicit_tensor_parallel_override_wins(engine):
    result = engine.render(V1_RECIPE, params={"tensor_parallel": 4}, topology=ONE_NODE)
    assert result.command == BASE.format(tp=4) + rendezvous(1, 0)


def test_two_node_rank0_appends_rendezvous_args(engine):
    result = engine.render(V1_RECIPE, topology=TWO_NODES, node_rank=0)
    assert result.command == BASE.format(tp=2) + rendezvous(2, 0)
    assert result.host == "spark-a"


def test_two_node_rank1_is_headless(engine):
    result = engine.render(V1_RECIPE, topology=TWO_NODES, node_rank=1)
    assert result.command == BASE.format(tp=2) + rendezvous(2, 1)
    assert result.host == "spark-b"


def test_three_node_ranks(engine):
    """Every rank above zero is headless, and the head address never moves."""
    for rank, host in enumerate(("spark-a", "spark-b", "spark-c")):
        result = engine.render(V1_RECIPE, topology=THREE_NODES, node_rank=rank)
        assert result.command == BASE.format(tp=2) + rendezvous(3, rank)
        assert result.host == host


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
    result = engine.render(recipe, topology=ONE_NODE)
    assert result.command == (
        "vllm serve M --port 8000 --override '{\"a\": 1}'" + rendezvous(1, 0)
    )


def test_missing_placeholder_raises_clear_error(engine):
    recipe = {**V1_RECIPE, "command": "vllm serve M --port {port} --hey {nope}"}
    with pytest.raises(EngineError) as exc:
        engine.render(recipe)
    assert "{nope}" in str(exc.value)
    assert "available params" in str(exc.value)


PINNED = (
    "MN_IF_NAME",
    "UCX_NET_DEVICES",
    "NCCL_SOCKET_IFNAME",
    "OMPI_MCA_btl_tcp_if_include",
    "TP_SOCKET_IFNAME",
    "NCCL_IB_HCA",
)


def test_one_node_pins_no_interface(engine):
    """The one genuine difference at a single node.

    The node carries real interface names, and they must still not be pinned:
    these variables are find-or-fail, and a single node never touches the
    fabric.
    """
    env = engine.render(V1_RECIPE, topology=ONE_NODE).env
    for key in PINNED:
        assert key not in env
    assert env["GLOO_SOCKET_IFNAME"] == "lo"
    assert env["VLLM_HOST_IP"] == "10.0.0.1"


def test_more_than_one_node_pins_the_fabric(engine):
    for topology in (TWO_NODES, THREE_NODES):
        env = engine.render(V1_RECIPE, topology=topology).env
        for key in PINNED:
            assert env[key], key
        assert env["GLOO_SOCKET_IFNAME"] == "enp1s0f0np0"


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


def test_version_guard_refuses_an_image_older_than_the_flags(engine):
    """The rendezvous flags are stock upstream only from vLLM 0.11.1."""
    engine.spec.framework_version = "0.10.2"
    ok, reason = engine.version_supported()
    assert ok is False
    assert "0.10.2" in reason
    assert "0.11.1" in reason


def test_version_guard_accepts_the_declared_image(engine):
    assert engine.framework_version() == "0.28.1"
    assert engine.version_supported() == (True, "")


def test_version_guard_accepts_the_first_supported_release(engine):
    engine.spec.framework_version = "0.11.1"
    assert engine.version_supported() == (True, "")


def test_version_guard_reads_a_dev_build(engine):
    engine.spec.framework_version = "0.11.0rc2.dev12+g4cc0cb6f7"
    ok, _ = engine.version_supported()
    assert ok is False


def test_undeclared_version_warns_rather_than_refusing(engine):
    engine.spec.framework_version = ""
    ok, reason = engine.version_supported()
    assert ok is True
    assert "0.11.1" in reason


def test_version_falls_back_to_the_pinned_source(engine):
    engine.spec.framework_version = ""
    engine.spec.sources = {"vllm": {"version": "0.9.0"}}
    ok, reason = engine.version_supported()
    assert ok is False
    assert "0.9.0" in reason


def test_v2_recipe_without_command(engine):
    recipe = {
        "id": "generic",
        "model": "Qwen/Qwen3-8B",
        "recipe_version": "2",
        "params": {"host": "0.0.0.0", "port": 9000, "max_model_len": 4096},
        "engines": {"vllm": {"args": "--enable-prefix-caching"}},
    }
    result = engine.render(recipe, topology=ONE_NODE)
    assert result.command == (
        # No --tensor-parallel-size: the recipe names none, and one node no
        # longer injects one.
        "vllm serve Qwen/Qwen3-8B --host 0.0.0.0 --port 9000 "
        "--max-model-len 4096 --enable-prefix-caching" + rendezvous(1, 0)
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
