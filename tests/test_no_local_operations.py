"""One path to a node, including the node this process runs on.

The control plane coordinates; the agent on each node executes. That is the
whole design of `docs/cluster-agent-plan.md`, and `node_service.service_for`
holds up its half — it has no local branch, because this process runs an agent
for itself and reaches it over loopback exactly as it reaches a peer.

What that guarantee is worth depends on callers actually using it. Two did not.
`native_runtime.rank_services` sent the empty address — the record's sentinel
for "this machine" — straight to `tools.docker`, so rank zero of a solo
deployment drove Docker from Python while rank one of a two-node deployment
drove it through an agent: two implementations of one operation, and the
untested one was the one nearly every install runs. `tools.images` did the same
for every pull, list, inspect and delete.

These tests are the ratchet. They read the source, because the property is
about which module a call goes to rather than what it returns, and a runtime
assertion would only fire on the paths a test already covers.

Reading a machine's own hardware used to be the exception here: `tools.system`
shelled out to `nvidia-smi` and `/proc` for the monitoring panels, and the
page was single-node as a result. That is gone — `GetNodeStats` is a command
now, so every node answers for itself and the control node is not a special
case. What remains outside is `tools.preflight`, whose probes run over the
probe's own transport, and `tools.discovery`, which enumerates this host's
interfaces before there is any node to ask.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "spark_pulse" / "tools"

#: Modules allowed to reach the container service without a node.
#:
#: ``docker`` *is* that service, and ``node_service`` is what binds it to a
#: node — the two ends of the seam. Everything else goes through the seam.
MAY_TOUCH_DOCKER_DIRECTLY = {"docker.py", "node_service.py"}

#: Modules that legitimately shell out, and what for.
#:
#: Each is about *this machine as a machine* rather than as a node: its
#: hardware, its network, its SSH client, its registry daemon. A node-scoped
#: operation belongs on the agent instead.
MAY_USE_SUBPROCESS = {
    "discovery.py": "this host's own interfaces and RoCE devices",
    "docker.py": "the Docker client itself",
    "preflight.py": "probe commands, which run over the probe's own transport",
    "registry.py": "the local registry container",
    "ssh.py": "the SSH transport, used for bootstrap only",
    "models.py": "the hub-cache verifier it still ships to peers over SSH",
}


def _tool_modules() -> list[Path]:
    return sorted(p for p in TOOLS.glob("*.py") if p.name != "__init__.py")


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _dotted(node: ast.AST) -> str:
    """``tools.docker._get_service`` for an attribute chain, else ""."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


@pytest.mark.parametrize("module", _tool_modules(), ids=lambda p: p.name)
def test_no_tool_reaches_the_container_service_without_a_node(module: Path):
    """A container operation names the node it happens on.

    ``tools.docker._get_service()`` is this process's own Docker client. Using
    it is a local operation by construction: there is no node in the call, so
    there is nothing to send to an agent.
    """
    if module.name in MAY_TOUCH_DOCKER_DIRECTLY:
        pytest.skip(f"{module.name} is the seam itself")

    tree = ast.parse(module.read_text(encoding="utf-8"))
    offenders = [
        _dotted(call.func)
        for call in _calls(tree)
        if _dotted(call.func).endswith("docker._get_service")
    ]

    assert offenders == [], (
        f"{module.name} reaches this process's Docker client directly "
        f"({', '.join(offenders)}). Container work goes through "
        "`node_service`, which routes even this machine through its own "
        "agent — that is what makes a solo deployment and a rank of a "
        "cluster the same code path."
    )


@pytest.mark.parametrize("module", _tool_modules(), ids=lambda p: p.name)
def test_only_the_named_modules_shell_out(module: Path):
    """Shelling out is how a local operation gets written by accident.

    The allowed list is small and each entry says what it is for. Adding to it
    is a decision; adding a `subprocess.run` to a module that has never had one
    should not be an accident.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    uses = {
        _dotted(call.func)
        for call in _calls(tree)
        if _dotted(call.func).startswith(("subprocess.", "os.system", "os.popen"))
    }

    if module.name in MAY_USE_SUBPROCESS:
        return
    assert uses == set(), (
        f"{module.name} shells out ({', '.join(sorted(uses))}) and is not in "
        "MAY_USE_SUBPROCESS. If it operates on a node, send it to that node's "
        "agent; if it reads this machine's own hardware, add it to the list "
        "with the reason."
    )


def test_the_node_resolver_has_no_local_branch():
    """`service_for` must not special-case this machine.

    The guarantee every caller relies on is that one function answers for
    every node. A branch here would put the control node back on a second
    implementation, which is what the agent work removed.
    """
    source = (TOOLS / "node_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "service_for"
    )

    body = ast.get_source_segment(source, function) or ""

    assert "docker._get_service" not in body
    assert "is_local_address" not in body
    assert "is_self" not in body


def test_rank_services_sends_every_rank_through_the_resolver():
    """Including rank zero of a solo deployment, whose address is empty.

    ``is_local_address("")`` is true — the empty string is in
    ``LOOPBACK_ADDRESSES`` — so the resolver already answers for it. The branch
    that used to intercept it never reached an agent at all.
    """
    source = (TOOLS / "native_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "rank_services"
    )

    body = ast.get_source_segment(source, function) or ""

    assert "if not address" not in body, (
        "rank_services intercepts the empty address again. That is rank zero "
        "of a solo deployment, and routing it past the resolver is what made "
        "a single-node install a different code path from a cluster."
    )


def test_the_empty_address_is_this_machine():
    """The premise the removed branch was standing on, asserted directly."""
    from spark_pulse.tools import node_service

    assert node_service.is_local_address("") is True
    assert node_service.node_for("").is_self is True
