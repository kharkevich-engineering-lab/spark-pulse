"""llama.cpp, solo and across machines over its RPC backend.

**Nothing here is evidence that it works on hardware.** What these tests hold
is what simulation can witness: that the bundled ``llama-cpp`` spec renders
exactly what it rendered before and still refuses a second node, that a
variant declaring ``multi_node: {style: llama-rpc}`` renders a head naming
every worker and a worker running nothing but the RPC server, and that a spec
which claims the size without declaring the style is refused with the fix in
the message rather than rendered as something plausible.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from spark_pulse.engines import EngineError, EngineSpec, NodeInfo, Topology
from spark_pulse.engines.llama_cpp import (
    DEFAULT_RPC_PORT,
    RPC_SERVER,
    RPC_STYLE,
    LlamaCppEngine,
    rpc_endpoint,
)
from spark_pulse.engines.registry import ENGINE_CLASSES, load_bundled_specs

MODEL = "PrismML/Bonsai-2-27B-GGUF"
RECIPE = {
    "id": "bonsai-2-27b",
    "model": MODEL,
    "recipe_version": "2",
    "params": {"max_model_len": 8192, "max_num_seqs": 4},
}

HEAD = NodeInfo(host="spark-a", ip="10.0.0.11", eth_if="enp1s0f0np0", ib_if="rocep1s0")
WORKER = NodeInfo(host="spark-b", ip="10.0.0.12", eth_if="enp1s0f0np0")
THIRD = NodeInfo(host="spark-c", ip="10.0.0.13", eth_if="enp1s0f0np0")

ONE_NODE = Topology(nodes=[HEAD])
TWO_NODES = Topology(nodes=[HEAD, WORKER])
THREE_NODES = Topology(nodes=[HEAD, WORKER, THIRD])


def spec_for(variant: str) -> EngineSpec:
    return next(
        s
        for s in load_bundled_specs()
        if s.engine == "llama-cpp" and s.variant == variant
    )


def engine(variant: str = "prism", **spec_overrides: Any) -> LlamaCppEngine:
    """The bundled spec for a variant, optionally bent to make a point."""
    spec = spec_for(variant)
    if spec_overrides:
        payload = copy.deepcopy(spec.model_dump())
        for dotted, value in spec_overrides.items():
            target = payload
            *path, last = dotted.split(".")
            for key in path:
                target = target[key]
            target[last] = value
        spec = EngineSpec.model_validate(payload)
    return LlamaCppEngine(spec)


# ── The bundled engine is unchanged ──────────────────────────────────────────


def test_the_default_variant_renders_exactly_the_solo_command_it_did_before():
    result = engine("default").render(RECIPE, params={"port": 9000}, topology=ONE_NODE)

    assert result.command == (
        f"llama-server --metrics -hf {MODEL} --host 0.0.0.0 --port 9000 "
        "--ctx-size 8192 --parallel 4"
    )
    assert result.node_rank == 0
    assert result.host == "spark-a"


def test_the_default_variant_still_refuses_a_second_node():
    """It declares cluster: false and no style, and both are refusals. The
    engine gaining an RPC renderer must not quietly extend to a spec whose
    image may not even carry the server binary."""
    ok, reason = engine("default").supports_size(2)
    assert ok is False and "cluster: false" in reason

    with pytest.raises(EngineError, match="one node"):
        engine("default").render(RECIPE, topology=TWO_NODES)


def test_a_claimed_cluster_with_no_style_is_refused_and_says_what_to_declare():
    """The other half of the claim. ``cluster: true`` alone is a spec saying a
    size it has nothing to render for."""
    solo_style = engine("prism", **{"runtime.multi_node": {"style": "none"}})

    ok, reason = solo_style.supports_size(2)
    assert ok is False
    assert RPC_STYLE in reason

    with pytest.raises(EngineError, match=RPC_STYLE):
        solo_style.render(RECIPE, topology=TWO_NODES)


def test_the_rpc_style_on_a_spec_that_does_not_claim_the_cluster_is_refused():
    """Style and claim are separate, and the claim is what decides."""
    refuses = engine("prism", **{"capabilities.cluster": False})

    ok, reason = refuses.supports_size(2)
    assert ok is False and "cluster: false" in reason

    with pytest.raises(EngineError, match="cluster: false"):
        refuses.render(RECIPE, topology=TWO_NODES)


def test_three_nodes_need_the_mesh_claim_like_every_other_engine():
    ok, reason = engine("prism").supports_size(3)
    assert ok is False and "mesh: false" in reason


# ── The RPC gang ─────────────────────────────────────────────────────────────


def test_the_prism_variant_is_solo_when_it_is_given_one_node():
    """The style is what a *topology* is rendered through, not a mode the
    engine is in: one node is one ``llama-server`` and no --rpc at all."""
    result = engine().render(RECIPE, topology=ONE_NODE)

    assert "--rpc" not in result.command
    assert result.command.startswith("llama-server --metrics")
    assert "--port 8080" in result.command


def test_the_head_names_every_worker_on_one_rpc_flag():
    result = engine().render(RECIPE, params={"port": 9000}, topology=TWO_NODES)

    assert result.command == (
        f"llama-server --metrics -hf {MODEL} --host 0.0.0.0 --port 9000 "
        "--ctx-size 8192 --parallel 4 --rpc 10.0.0.12:50052"
    )
    assert result.node_rank == 0
    assert result.host == "spark-a"


def test_a_three_node_head_lists_both_workers_in_rank_order():
    """Three nodes needs the mesh claim as it does for every other engine, so
    this is a spec that makes it — the shape of the flag is what is under
    test, and there is one ``--rpc`` with the workers in rank order on it."""
    meshed = engine("prism", **{"capabilities.mesh": True})

    result = meshed.render(RECIPE, topology=THREE_NODES)

    assert "--rpc 10.0.0.12:50052,10.0.0.13:50052" in result.command
    assert result.command.count("--rpc") == 1


def test_a_worker_runs_the_rpc_server_and_nothing_else():
    """No model, no serve flags, no extra args: none of them are arguments
    ``ggml-rpc-server`` knows, and it would exit on the first one."""
    result = engine().render(
        RECIPE,
        params={"port": 9000},
        extra_args=["--flash-attn", "on"],
        topology=TWO_NODES,
        node_rank=1,
    )

    assert result.command == f"{RPC_SERVER} -H 0.0.0.0 -p 50052"
    assert result.node_rank == 1
    assert result.host == "spark-b"
    assert MODEL not in result.script


def test_the_rpc_port_comes_from_the_spec_and_falls_back_to_upstreams():
    named = engine("prism", **{"runtime.ports": {"api": 8080, "rpc": 50099}})
    assert named.rpc_port() == 50099
    assert "-p 50099" in named.render(RECIPE, topology=TWO_NODES, node_rank=1).command

    unnamed = engine("prism", **{"runtime.ports": {"api": 8080}})
    assert unnamed.rpc_port() == DEFAULT_RPC_PORT

    # Solo variants bind nothing: there is no worker to run a server.
    assert engine("default").rpc_port() is None


def test_a_rank_outside_the_topology_is_refused():
    with pytest.raises(EngineError, match="out of range"):
        engine().render(RECIPE, topology=TWO_NODES, node_rank=2)


def test_the_head_can_be_given_the_offload_flag_by_the_spec():
    """``-ngl`` is what makes the head put layers on the RPC devices at all.
    It is not rendered here: it belongs to the image's own defaults, the
    recipe, or the spec's multi_node.extra_args — and that last one is a
    published spec's way of saying it, so the renderer honours it."""
    offloads = engine(
        "prism",
        **{"runtime.multi_node": {"style": RPC_STYLE, "extra_args": ["-ngl", "99"]}},
    )

    command = offloads.render(RECIPE, topology=TWO_NODES).command

    assert command.endswith("--rpc 10.0.0.12:50052 -ngl 99")
    # And never on a worker, which parses none of it.
    assert (
        "-ngl" not in offloads.render(RECIPE, topology=TWO_NODES, node_rank=1).command
    )


def test_a_recipes_own_args_still_come_after_the_rpc_flag():
    """What an operator wrote stays last, so it can still override."""
    recipe = {**RECIPE, "args": "--parallel 8"}

    command = engine().render(recipe, topology=TWO_NODES).command

    assert command.endswith("--rpc 10.0.0.12:50052 --parallel 8")


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_both_variants_are_rendered_by_the_one_class():
    """Keyed by engine name, so a variant cannot arrive with no renderer."""
    assert ENGINE_CLASSES["llama-cpp"] is LlamaCppEngine
    assert isinstance(engine("default"), LlamaCppEngine)
    assert isinstance(engine("prism"), LlamaCppEngine)


def test_the_launch_states_no_parallelism_for_the_capacity_check_to_read():
    """There is no -tp on either rank: the shape is the --rpc list, one
    server per worker. A capacity check that read the command would see a
    one-GPU launch on two nodes and refuse a deploy that is correct, which is
    what this flag exists to prevent."""
    assert LlamaCppEngine.parallelism_in_command is False
    assert "-tp" not in engine().render(RECIPE, topology=TWO_NODES).command


def test_the_prism_spec_declares_what_the_renderer_reads():
    spec = spec_for("prism")

    assert spec.key == "llama-cpp/prism"
    assert spec.capabilities.cluster is True
    assert spec.capabilities.mesh is False
    assert spec.runtime.multi_node.style == RPC_STYLE
    assert spec.runtime.ports.api == 8080
    assert spec.runtime.ports.rpc == DEFAULT_RPC_PORT
    assert spec.runtime.container.network_host is True
    assert spec.sources["llama_cpp"]["repo"].endswith("PrismML-Eng/llama.cpp.git")


# ── Which wire the head dials ────────────────────────────────────────────────
#
# Every node above carries no fabric address, which is what a fleet looks like
# before anyone has run a fabric apply — so every test above it renders the
# registered address, unchanged. These are about the other case.

#: A verified apply's work: NETWORKING.md's scheme, one /24 per cable.
FABRIC_HEAD = NodeInfo(
    host="spark-a",
    ip="10.0.0.11",
    eth_if="enp1s0f0np0",
    ib_if="rocep1s0",
    fabric_addresses=("192.168.177.11", "192.168.178.11"),
)
FABRIC_WORKER = NodeInfo(
    host="spark-b",
    ip="10.0.0.12",
    eth_if="enp1s0f0np0",
    fabric_addresses=("192.168.177.12", "192.168.178.12"),
)
FABRIC_THIRD = NodeInfo(
    host="spark-c",
    ip="10.0.0.13",
    eth_if="enp1s0f0np0",
    fabric_addresses=("192.168.177.13", "192.168.178.13"),
)


def test_the_fabric_address_sharing_the_heads_subnet_is_the_one_chosen():
    address, reason = rpc_endpoint(FABRIC_HEAD, FABRIC_WORKER, DEFAULT_RPC_PORT)

    assert address == "192.168.177.12"
    assert "ConnectX fabric this control plane verified" in reason
    assert "192.168.177.11" in reason


def test_a_worker_with_no_verified_fabric_address_keeps_the_registered_one():
    address, reason = rpc_endpoint(FABRIC_HEAD, WORKER, DEFAULT_RPC_PORT)

    assert address == WORKER.address()
    assert "no verified fabric address is recorded for spark-b" in reason
    assert "run a fabric apply from Fleet" in reason


def test_a_head_with_no_verified_fabric_address_has_no_cable_to_match():
    """Both ends or neither: an endpoint is a pair, and a worker address on
    a cable the head is not addressed on is not reachable from the head."""
    address, reason = rpc_endpoint(HEAD, FABRIC_WORKER, DEFAULT_RPC_PORT)

    assert address == FABRIC_WORKER.address()
    assert "no verified fabric address is recorded for the head spark-a" in reason


def test_a_fabric_address_on_another_cable_is_not_a_cable_to_this_head():
    """The mesh case that must not be guessed: an address exists, it is
    verified, and it faces a different machine."""
    elsewhere = NodeInfo(
        host="spark-c", ip="10.0.0.13", fabric_addresses=("192.168.197.13",)
    )

    address, reason = rpc_endpoint(FABRIC_HEAD, elsewhere, DEFAULT_RPC_PORT)

    assert address == "10.0.0.13"
    assert "no cable runs between them" in reason
    assert "192.168.197.13" in reason


def test_the_mesh_worker_holds_one_address_per_cable_and_the_heads_wins():
    """A ring member is addressed on two cables. Only one of them runs to the
    head, and it is the one the head is addressed on too — the second, here,
    so that a renderer that simply took the first would fail this."""
    head = NodeInfo(
        host="spark-a", ip="10.0.0.11", fabric_addresses=("192.168.187.11",)
    )
    worker = NodeInfo(
        host="spark-b",
        ip="10.0.0.12",
        fabric_addresses=("192.168.197.12", "192.168.187.12"),
    )

    address, _ = rpc_endpoint(head, worker, DEFAULT_RPC_PORT)

    assert address == "192.168.187.12"


def test_something_that_is_not_an_address_is_never_matched():
    """A registry record is data. A malformed entry falls back rather than
    being compared as a string."""
    worker = NodeInfo(host="spark-b", ip="10.0.0.12", fabric_addresses=("not-an-ip",))

    address, reason = rpc_endpoint(FABRIC_HEAD, worker, DEFAULT_RPC_PORT)

    assert address == "10.0.0.12"
    assert "no cable runs between them" in reason


def test_the_head_command_names_the_fabric_addresses_not_the_registered_ones():
    result = engine().render(
        RECIPE, topology=Topology(nodes=[FABRIC_HEAD, FABRIC_WORKER])
    )

    assert "--rpc 192.168.177.12:50052" in result.command
    assert "10.0.0.12" not in result.command


def test_three_nodes_list_two_endpoints_in_rank_order():
    topology = Topology(nodes=[FABRIC_HEAD, FABRIC_WORKER, FABRIC_THIRD])

    endpoints = engine().rpc_endpoints(topology)

    assert [e["node"] for e in endpoints] == ["10.0.0.12", "10.0.0.13"]
    assert [e["address"] for e in endpoints] == ["192.168.177.12", "192.168.177.13"]
    assert all(e["via_fabric"] for e in endpoints)
    # Three nodes needs the mesh claim, as it does for every other engine.
    meshed = engine("prism", **{"capabilities.mesh": True})
    assert (
        "--rpc 192.168.177.12:50052,192.168.177.13:50052"
        in meshed.render(RECIPE, topology=topology).command
    )


def test_the_reported_endpoints_are_what_the_command_was_rendered_from():
    topology = Topology(nodes=[FABRIC_HEAD, WORKER])

    endpoints = engine().rpc_endpoints(topology)

    assert endpoints == [
        {
            "node": "10.0.0.12",
            "address": "10.0.0.12",
            "port": DEFAULT_RPC_PORT,
            "via_fabric": False,
            "reason": endpoints[0]["reason"],
        }
    ]
    assert (
        f"--rpc {endpoints[0]['address']}:{endpoints[0]['port']}"
        in engine().render(RECIPE, topology=topology).command
    )


def test_nothing_that_does_not_span_nodes_reports_an_endpoint():
    """A solo launch dials nobody, and neither does a rendezvous engine —
    the base class answers with an empty list for exactly that reason."""
    assert engine().rpc_endpoints(ONE_NODE) == []
    assert engine("default").rpc_endpoints(TWO_NODES) == []
