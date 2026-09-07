"""The five single-node engines, rendered from their specs and nothing else.

There is one renderer for all of them (``spark_pulse.engines.solo``), so what
is worth testing is not five copies of the same string: it is that each spec
produces the launch its own engine documents, that the shared renderer refuses
what it cannot render rather than producing something plausible, and that a
model reaches the command in whichever way that engine takes one.
"""

import pytest

from spark_pulse.engines import (
    AtlasEngine,
    EngineError,
    LlamaCppEngine,
    ModularMaxEngine,
    NodeInfo,
    SoloEngine,
    Topology,
    TrtllmEngine,
)
from spark_pulse.engines.registry import ENGINE_CLASSES, load_bundled_specs

MODEL = "unsloth/Qwen3-8B-GGUF"
RECIPE = {
    "id": "qwen3-8b",
    "model": MODEL,
    "recipe_version": "2",
    "params": {"max_model_len": 8192, "max_num_seqs": 4},
}

ONE_NODE = Topology(nodes=[NodeInfo(host="spark-a", ip="10.0.0.1")])
TWO_NODES = Topology(
    nodes=[
        NodeInfo(host="spark-a", ip="10.0.0.1"),
        NodeInfo(host="spark-b", ip="10.0.0.2"),
    ]
)

#: Every engine the solo renderer serves, with the whole command each spec
#: should produce for :data:`RECIPE` at ``--port 9000``.
CASES = {
    "llama-cpp": (
        LlamaCppEngine,
        f"llama-server --metrics -hf {MODEL} --host 0.0.0.0 --port 9000 "
        "--ctx-size 8192 --parallel 4",
    ),
    "trtllm": (
        TrtllmEngine,
        f"trtllm-serve {MODEL} --host 0.0.0.0 --port 9000 "
        "--max_seq_len 8192 --max_batch_size 4",
    ),
    "modular-max": (
        ModularMaxEngine,
        f"max serve --model {MODEL} --host 0.0.0.0 --port 9000 "
        "--max-length 8192 --max-batch-size 4",
    ),
    "atlas": (
        AtlasEngine,
        f"spark serve {MODEL} --host 0.0.0.0 --port 9000 "
        "--max-seq-len 8192 --max-num-seqs 4",
    ),
}


def engine_for(name: str) -> SoloEngine:
    cls = CASES[name][0]
    spec = next(s for s in load_bundled_specs() if s.engine == name)
    return cls(spec)


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_engine_renders_the_launch_its_own_docs_show(name):
    result = engine_for(name).render(RECIPE, params={"port": 9000}, topology=ONE_NODE)

    assert result.command == CASES[name][1]
    assert result.node_rank == 0
    assert result.host == "spark-a"


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_one_of_them_is_registered_under_its_own_name(name):
    """A spec whose engine name has no plugin cannot be launched at all, and
    the failure arrives at deploy time rather than here."""
    assert ENGINE_CLASSES[name] is CASES[name][0]


def test_the_model_reaches_the_command_however_that_engine_takes_one():
    """Three shapes across the five: a positional argument, a named flag, and
    llama.cpp's ``-hf``, which resolves a GGUF repository rather than reading
    a path."""
    assert " -hf " in engine_for("llama-cpp").render(RECIPE).command
    assert f"trtllm-serve {MODEL}" in engine_for("trtllm").render(RECIPE).command
    assert f"--model {MODEL}" in engine_for("modular-max").render(RECIPE).command


def test_a_missing_model_is_refused_rather_than_launched_empty():
    with pytest.raises(EngineError, match="no model"):
        engine_for("atlas").render({"id": "x", "recipe_version": "2"})


def test_more_than_one_node_is_refused_with_what_to_do_instead():
    """Every one of these declares cluster: false, so a two-node deployment is
    rejected before rendering. If it ever reached the renderer, a command for
    one node would be the worse answer."""
    with pytest.raises(EngineError, match="one node"):
        engine_for("trtllm").render(RECIPE, topology=TWO_NODES)


def test_a_rank_above_zero_is_refused():
    with pytest.raises(EngineError, match="out of range"):
        engine_for("atlas").render(RECIPE, topology=ONE_NODE, node_rank=1)


def test_the_engines_port_is_the_default_when_the_recipe_names_none():
    """Not 8000 for all of them: llama-server listens on 8080 and Atlas on
    8888, and the spec is what says so."""
    assert "--port 8080" in engine_for("llama-cpp").render(RECIPE).command
    assert "--port 8888" in engine_for("atlas").render(RECIPE).command
    assert "--port 8000" in engine_for("trtllm").render(RECIPE).command


def test_a_param_the_engine_has_no_flag_for_is_dropped_not_guessed():
    """llama.cpp has no tensor parallelism and nothing that means
    gpu_memory_utilization. Mapping either onto some other flag would be worse
    than leaving it out, so param_flags names neither and both disappear."""
    command = (
        engine_for("llama-cpp")
        .render(RECIPE, params={"tensor_parallel": 2, "gpu_memory_utilization": 0.9})
        .command
    )

    assert "--tensor-parallel" not in command and "--tp" not in command
    assert "0.9" not in command
    assert command == CASES["llama-cpp"][1].replace("--port 9000", "--port 8080")


def test_extra_args_are_appended_quoted():
    command = (
        engine_for("atlas")
        .render(
            RECIPE, extra_args=["--kv-cache-dtype", "nvfp4", "--warmup-prompt", "a b"]
        )
        .command
    )

    assert command.endswith("--kv-cache-dtype nvfp4 --warmup-prompt 'a b'")


def test_a_recipe_pinned_to_another_engine_is_not_rendered():
    """A top-level `command:` is written in one engine's flags."""
    recipe = {"model": MODEL, "command": "vllm serve {model} --max-model-len 8192"}

    with pytest.raises(EngineError, match="vllm"):
        engine_for("trtllm").render(recipe)


def test_a_single_node_launch_carries_no_fabric_pinning():
    """One machine never touches the fabric, and NCCL_SOCKET_IFNAME is
    find-or-fail: pinning an interface here is how a solo launch dies on a
    machine whose fabric link is down."""
    env = engine_for("modular-max").render(RECIPE, topology=ONE_NODE).env

    assert "NCCL_SOCKET_IFNAME" not in env
    assert "NCCL_IB_HCA" not in env
    assert env["GLOO_SOCKET_IFNAME"] == "lo"
    assert env["HF_HOME"] == "/root/.cache/huggingface"


def test_the_recipes_own_env_and_args_still_reach_the_launch():
    recipe = {
        **RECIPE,
        "args": "--scheduling-policy slai",
        "env": {"ATLAS_LOG": "debug"},
    }

    result = engine_for("atlas").render(recipe, topology=ONE_NODE)

    assert result.command.endswith("--scheduling-policy slai")
    assert result.env["ATLAS_LOG"] == "debug"
    assert "export ATLAS_LOG=" in result.script


def test_every_bundled_engine_declares_a_published_image():
    """`available` is what keeps an engine with no image out of the deploy
    options: pulling an unpublished reference answers 403, not an image. Every
    one bundled today has one — the engine that did not was removed rather
    than left as a definition nobody could run."""
    specs = {s.engine: s for s in load_bundled_specs()}

    assert specs["llama-cpp"].available is True
    assert specs["atlas"].available is True
    assert specs["trtllm"].available is True
