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
case. The pre-flight's host probes followed: `tools.preflight` sends each one
as a `RunHostProbe` command through the node's own agent, the control node over
loopback, so it no longer shells out or logs in. So did the caches: `tools.cache`
walked and emptied `~/.cache` in this process, which made the Library's Caches
section answer for the control node and call it the cluster, and it is now
`ScanCache`/`CleanCache` on every node's own agent. What remains outside is
`tools.discovery`, which enumerates this host's interfaces before there is any
node to ask.
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

#: Modules that legitimately write to *this* machine's filesystem, and why.
#:
#: The distinction is whose state it is. The control plane's own config,
#: caches and recipe directories are this process's to write; a node's Docker
#: images, its model cache and its containers are not, and reach it through
#: the agent. A module that deletes something on a node without naming the
#: node is deleting it here and reporting it as everywhere — which is how a
#: 26 GB model came to be removed from one Spark out of four while the page
#: said it was gone.
MAY_TOUCH_THE_FILESYSTEM = {
    "atomic_json.py": "the temp file it renames into place, by definition",
    "benchmarking.py": (
        "the temp file llama-benchy writes its JSON report to on this machine, "
        "read back and deleted here"
    ),
    "docker.py": "the executor itself — it runs *on* the node, as the agent",
    "launch_script.py": "a temp directory it owns, copied into containers",
    "ssh.py": (
        "its own directory under ~/.config/spark-pulse: the control sockets, "
        "and the known_hosts holding the keys bootstrap confirmed"
    ),
    "custom_files.py": "the operator's own recipe and mod directories",
    "hub_cache.py": "a standalone layout/verification module with no node in it",
    "oci_registry.py": "the control plane's own registry cache and recipe files",
    "native_runtime.py": "the temp launch script it hands to copy_to_container",
    "registry.py": "the local registry container's data directory",
}

#: Modules allowed to signal a process, and why.
#:
#: Empty, and that is the point. Every process this system ends goes through
#: `TerminateProcess` on the node that holds it, because the same pid on two
#: Sparks is two different processes and only one of them is on this machine.
#: The last exception was the pre-native record teardown, and it went with the
#: records it was for.
MAY_SIGNAL_A_PROCESS: dict[str, str] = {}

#: Modules that legitimately shell out, and what for.
#:
#: Each is about *this machine as a machine* rather than as a node: its
#: hardware, its network, its SSH client, its registry daemon. A node-scoped
#: operation belongs on the agent instead.
MAY_USE_SUBPROCESS = {
    "benchmarking.py": (
        "the llama-benchy load generator: a client of a deployment's own "
        "HTTP endpoint, run from this machine by design, never on a node"
    ),
    "discovery.py": "this host's own interfaces and RoCE devices",
    "docker.py": "the Docker client itself",
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
def test_no_tool_constructs_the_container_service_directly(module: Path):
    """Naming the node is not enough if the service is built without one.

    ``docker._get_service()`` is the obvious way to reach this process's own
    Docker client, and the test above forbids it. Constructing
    ``DockerService()`` directly is the same local operation wearing a
    different name: it binds to whatever daemon this machine has and never
    reaches an agent, so a call meant for a peer runs here instead. That is how
    ``reconciliation.py`` came to reap only this node's orphans while the
    ratchet stayed green — it built the client rather than calling the spelling
    the ratchet knew. Container work is built by the seam (`docker`,
    `node_service`) and reached through `node_service`, which routes even this
    machine through its own agent.
    """
    if module.name in MAY_TOUCH_DOCKER_DIRECTLY:
        pytest.skip(f"{module.name} is the seam itself")

    tree = ast.parse(module.read_text(encoding="utf-8"))
    offenders = sorted(
        _dotted(call.func)
        for call in _calls(tree)
        if _dotted(call.func).endswith("DockerService")
    )

    assert offenders == [], (
        f"{module.name} constructs the container service directly "
        f"({', '.join(offenders)}). A DockerService built here talks to this "
        "machine's Docker with no node in the call, so an operation meant for "
        "a peer runs locally instead. Go through `node_service.service_for`, "
        "which reaches even this machine through its own agent."
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


@pytest.mark.parametrize("module", _tool_modules(), ids=lambda p: p.name)
def test_only_the_named_modules_write_to_this_machine(module: Path):
    """Deleting something without naming a node deletes it here only.

    The failure this catches is not a crash — it is an answer. A model removed
    from the control node and reported as removed leaves 26 GB on every other
    Spark, and nothing on the page ever says so again.
    """
    if module.name in MAY_TOUCH_THE_FILESYSTEM:
        return

    tree = ast.parse(module.read_text(encoding="utf-8"))
    writes = set()
    for call in _calls(tree):
        dotted = _dotted(call.func)
        if dotted in {
            "shutil.rmtree",
            "shutil.move",
            "shutil.copytree",
            "os.remove",
            "os.unlink",
            "os.rmdir",
            "os.makedirs",
            "os.rename",
        }:
            writes.add(dotted)
        # `path.unlink()`, `path.write_text(...)`, `path.mkdir(...)` — the
        # method name alone, because the receiver is a Path built anywhere.
        if isinstance(call.func, ast.Attribute) and call.func.attr in {
            "rmtree",
            "unlink",
            "write_bytes",
            "write_text",
            "mkdir",
        }:
            writes.add(call.func.attr)

    assert writes == set(), (
        f"{module.name} writes to this machine's filesystem "
        f"({', '.join(sorted(writes))}) and is not in "
        "MAY_TOUCH_THE_FILESYSTEM. If the path is on a node, send the "
        "operation to that node's agent — `RemoveSnapshot` and `RemoveImage` "
        "already exist. If it is the control plane's own state, add it to the "
        "list with the reason."
    )


@pytest.mark.parametrize("module", _tool_modules(), ids=lambda p: p.name)
def test_only_the_named_modules_signal_a_process(module: Path):
    """A pid is meaningless without the machine it is on.

    The Monitoring page's kill button was an `os.kill` in this process, so it
    worked on exactly one machine out of however many the operator has, and
    the button on every other node's rows was a lie.
    """
    if module.name in MAY_SIGNAL_A_PROCESS:
        return

    tree = ast.parse(module.read_text(encoding="utf-8"))
    signals = {
        _dotted(call.func)
        for call in _calls(tree)
        if _dotted(call.func) in {"os.kill", "os.killpg", "signal.raise_signal"}
    }

    assert signals == set(), (
        f"{module.name} signals a process on this machine "
        f"({', '.join(sorted(signals))}). Processes live on nodes: send it "
        "through the node's agent with `terminate_process`."
    )


def test_the_machine_questions_are_answered_by_the_agent_everywhere():
    """`get_node_stats`, the snapshot ops and `terminate_process` are node ops.

    They are not container operations, so they are not in
    `NODE_SERVICE_METHODS`; the property that matters is that they travel the
    same resolver, so the control node answers them exactly as a peer does.
    Both implementations must have all of them, or one node's monitoring
    panel is a different code path from another's.
    """
    from spark_pulse.agent.sync_service import AgentNodeService
    from spark_pulse.mock.docker import MockDockerService
    from spark_pulse.tools import node_service

    for method in node_service.NODE_MACHINE_METHODS:
        assert callable(
            getattr(AgentNodeService, method, None)
        ), f"the agent-backed service cannot answer {method}"
        assert callable(
            getattr(MockDockerService, method, None)
        ), f"the simulated node cannot answer {method}"


def test_nothing_asks_this_machine_about_its_own_gpu():
    """The monitoring path used to read `nvidia-smi` in this process.

    That is what made the page single-node: three of four Sparks were
    invisible and the visible one was unlabelled. The reading lives in the
    agent now, so no module here may name the tool.
    """
    # The pre-flight *names* nvidia-smi in a probe command, but it ships that
    # command to the node's agent (`RunHostProbe`) rather than running it here
    # — the string is data on the wire, not a local read. It is safe to exempt
    # from this string match because `test_only_the_named_modules_shell_out`
    # already forbids it any `subprocess`/`os.system` of its own, so it cannot
    # run the tool locally even if it holds its name.
    ships_to_the_agent = {"preflight.py"}
    offenders = []
    for module in _tool_modules():
        source = module.read_text(encoding="utf-8")
        if module.name in MAY_USE_SUBPROCESS or module.name in ships_to_the_agent:
            continue
        for tool in ("nvidia-smi", "free -m", "df -B1"):
            # In a string that is *run*, not in prose about what used to be.
            if f'"{tool}' in source or f"'{tool}" in source:
                offenders.append(f"{module.name}: {tool}")

    assert offenders == [], (
        "these modules read this machine's hardware directly: "
        + ", ".join(offenders)
        + ". Ask the node — `GetNodeStats` answers for every one of them, "
        "including this machine."
    )


def test_every_exception_is_one_something_actually_needs():
    """An allowlist with dead entries is a claim nobody checked.

    Each name here is a module that *does* the thing, with a reason. A module
    that stopped needing the exception has to leave, or the list stops meaning
    what it says — which is how `system.py` sat in MAY_USE_SUBPROCESS for a
    release after the parsing it needed moved into the agent.
    """
    present = {module.name for module in _tool_modules()}
    stale = {
        name
        for group in (
            MAY_TOUCH_THE_FILESYSTEM,
            MAY_SIGNAL_A_PROCESS,
            MAY_USE_SUBPROCESS,
            MAY_TOUCH_DOCKER_DIRECTLY,
        )
        for name in group
        if name not in present
    }

    assert (
        stale == set()
    ), f"these modules no longer exist but are still excused: {sorted(stale)}"


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
