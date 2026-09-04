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

FLAGS = " --host 0.0.0.0 --port 30000 --mem-fraction-static 0.9"


def line(tp: int, nnodes: int, rank: int, addr: str) -> str:
    """The whole rendered command at any size."""
    tail = f" --nnodes {nnodes} --node-rank {rank} --dist-init-addr {addr}:50000"
    if nnodes > 1:
        tail += " --enable-dp-attention"
    return f"{SERVE} --tp {tp} --pp-size 1{FLAGS}{tail}"


@pytest.fixture
def engine():
    spec = next(s for s in load_bundled_specs() if s.engine == "sglang")
    return SglangEngine(spec)


def test_one_node_rendezvous_addr_is_loopback(engine):
    """SGLang reads --dist-init-addr at one node, so it must not be the fabric.

    A one-node topology now carries a real node with a real address; pointing
    the rendezvous at it would break whenever that link is down or unaddressed,
    and it is never the right address for a launch that talks to itself.
    """
    result = engine.render(RECIPE, topology=ONE_NODE)
    assert result.command == line(tp=1, nnodes=1, rank=0, addr="127.0.0.1")
    assert result.host == "spark-a"


def test_the_default_topology_is_one_node(engine):
    assert engine.render(RECIPE).command == line(
        tp=1, nnodes=1, rank=0, addr="127.0.0.1"
    )


def test_solo_omits_multi_node_extra_args(engine):
    assert "--enable-dp-attention" not in engine.render(RECIPE).command


def test_two_node_rank0(engine):
    result = engine.render(
        RECIPE, params={"tensor_parallel": 2}, topology=TWO_NODES, node_rank=0
    )
    assert result.command == line(tp=2, nnodes=2, rank=0, addr="10.0.0.1")
    assert result.host == "spark-a"


def test_two_node_rank1(engine):
    result = engine.render(
        RECIPE, params={"tensor_parallel": 2}, topology=TWO_NODES, node_rank=1
    )
    assert result.command == line(tp=2, nnodes=2, rank=1, addr="10.0.0.1")
    assert result.host == "spark-b"


def test_three_node_ranks(engine):
    for rank, host in enumerate(("spark-a", "spark-b", "spark-c")):
        result = engine.render(
            RECIPE, params={"tensor_parallel": 3}, topology=THREE_NODES, node_rank=rank
        )
        assert result.command == line(tp=3, nnodes=3, rank=rank, addr="10.0.0.1")
        assert result.host == host


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


def test_supports_accepts_a_recipe_that_only_defaults_to_vllm(engine):
    """`engine:` names the default engine; it does not pin the recipe."""
    recipe = {**RECIPE, "engine": "vllm", "engines": {"vllm": {}, "sglang": {}}}
    assert engine.supports(recipe) == (True, "")


def test_supports_rejects_a_recipe_that_names_only_vllm(engine):
    ok, reason = engine.supports({**RECIPE, "engine": "vllm"})
    assert ok is False
    assert "vllm" in reason


def test_supports_reads_the_flattened_engine_list(engine):
    """The API serves `engines` as a list of names, not a mapping."""
    ok, reason = engine.supports({**RECIPE, "engines": ["vllm"]})
    assert ok is False
    assert "only declares engines: vllm" in reason
    assert engine.supports({**RECIPE, "engines": ["vllm", "sglang"]}) == (True, "")


def test_engine_specific_args_from_a_flattened_payload(engine):
    """`engine_specs` keeps each engine's args once a recipe is flattened."""
    recipe = {
        **RECIPE,
        "engines": ["vllm", "sglang"],
        "engine_specs": {
            "vllm": {"args": "--enable-prefix-caching", "env": {"VLLM_ONLY": "1"}},
            "sglang": {"args": "--tool-call-parser qwen25", "env": {"SGL_ONLY": "1"}},
        },
        "env": {"VLLM_ONLY": "1"},
    }
    result = engine.render(recipe)
    assert result.command.endswith("--tool-call-parser qwen25")
    assert "--enable-prefix-caching" not in result.command
    # The flattened top-level env belongs to the recipe's default engine.
    assert result.env["SGL_ONLY"] == "1"
    assert "VLLM_ONLY" not in result.env


def test_models_endpoint_is_separate_from_readiness(engine):
    """Readiness is /health; the served model id comes from /v1/models."""
    assert engine.readiness_path() == "/health"
    assert engine.models_path() == "/v1/models"


def test_tiktoken_files_are_mounted_and_pointed_at(engine):
    assert "~/tiktoken_encodings" in engine.cache_mounts()
    assert engine.base_env()["TIKTOKEN_ENCODINGS_BASE"] == "/root/tiktoken_encodings"


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


def test_one_node_pins_no_interface(engine):
    """The one genuine difference at a single node — see Engine.pinning_env."""
    env = engine.render(RECIPE, topology=ONE_NODE).env
    assert "NCCL_SOCKET_IFNAME" not in env
    assert "NCCL_IB_HCA" not in env
    assert env["GLOO_SOCKET_IFNAME"] == "lo"


def test_more_than_one_node_pins_the_fabric(engine):
    for topology in (TWO_NODES, THREE_NODES):
        env = engine.render(RECIPE, topology=topology).env
        assert env["NCCL_SOCKET_IFNAME"] == "enp1s0f0np0"
        assert env["GLOO_SOCKET_IFNAME"] == "enp1s0f0np0"
        assert env["NCCL_IB_HCA"] == "mlx5_0"


def test_no_version_demand(engine):
    """SGLang's flags have always been there; nothing to refuse."""
    assert engine.min_framework_version == ()
    assert engine.framework_version() == "0.5.10.post1"
    assert engine.version_supported() == (True, "")


def test_declarative_accessors(engine):
    assert engine.readiness_path() == "/health"
    assert engine.api_port() == 30000
    assert engine.rendezvous_port() == 50000
    assert engine.supports_mods() is False
    profile = engine.container_profile()
    assert profile["privileged"] is False
    assert profile["shm_size_gb"] == 32
    assert profile["devices"] == ["/dev/infiniband"]
    assert profile["ipc_host"] is True
    assert profile["network_host"] is True
    assert profile["ulimits"] == {"memlock": "-1", "stack": "67108864"}
    assert engine.default_image().endswith("/sglang:0.1.0")
