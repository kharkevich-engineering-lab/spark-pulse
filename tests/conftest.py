"""Shared pytest setup.

Make sure the checkout the tests live in is what gets imported, even when a
`spark-pulse` editable install elsewhere in the environment points at a
different working tree.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def isolate_imported_recipes(tmp_path, monkeypatch):
    """Keep recipe listing away from the developer's real ~/.config import dir.

    Both the real and the mock importer read the real module's ``IMPORTED_DIR``
    (the mock re-exports the path helpers), so patching it there covers both.
    """
    import spark_pulse.tools.recipe_import  # noqa: F401

    real = sys.modules["spark_pulse.tools.recipe_import"]
    monkeypatch.setattr(real, "IMPORTED_DIR", tmp_path / "_imported")


@pytest.fixture(autouse=True)
def isolate_managed_recipe_dirs(tmp_path, monkeypatch):
    """Keep recipe listing away from the developer's real config directory.

    ``recipe_sources`` lists the custom-recipes and OCI-recipes directories as
    first-class sources — they used to reach it only through symlinks planted
    in a checkout — so a developer with either of those populated would see
    their own recipes in every listing assertion.
    """
    import spark_pulse.tools.custom_files  # noqa: F401
    import spark_pulse.tools.oci_registry  # noqa: F401

    custom = sys.modules["spark_pulse.tools.custom_files"]
    oci = sys.modules["spark_pulse.tools.oci_registry"]
    monkeypatch.setattr(custom, "_CUSTOM_RECIPES_DIR", tmp_path / "_custom-recipes")
    monkeypatch.setattr(custom, "_CUSTOM_MODS_DIR", tmp_path / "_custom-mods")
    monkeypatch.setattr(oci, "RECIPES_DIR", tmp_path / "_oci-recipes")


@pytest.fixture(autouse=True)
def reset_simulated_registry():
    """Keep the process-wide simulated control-node registry per-test.

    ``spark_pulse.mock.registry`` holds one registry for the process, the way
    the real one holds one container per machine. Without this, what one test
    seeded would still be there for the next, and "the node already has it"
    would pass for the wrong reason.
    """
    from spark_pulse.mock import registry as mock_registry

    mock_registry.reset()
    yield
    mock_registry.reset()


@pytest.fixture(autouse=True)
def reset_simulated_node_registry():
    """Keep the process-wide simulated node registry per-test.

    ``spark_pulse.mock.node_registry`` holds one registry for the process, the
    way the real one holds one file per machine, and ``mock.discovery`` keeps
    the mDNS observations that feed the hostname-churn diagnostic. Without this
    a node one test added would still be there for the next.
    """
    from spark_pulse.mock import discovery as mock_discovery
    from spark_pulse.mock import node_registry as mock_node_registry

    mock_node_registry.reset()
    mock_discovery.reset_mock_discovery()
    yield
    mock_node_registry.reset()
    mock_discovery.reset_mock_discovery()


@pytest.fixture(autouse=True)
def reset_simulated_preflight():
    """Keep simulated unreachability from leaking between tests.

    ``spark_pulse.mock.preflight.UNREACHABLE`` is how a test says "this node is
    off". Left set, the next test's pre-flight reports a blocked verdict for a
    node it never touched.
    """
    from spark_pulse.mock import preflight as mock_preflight

    mock_preflight.reset()
    yield
    mock_preflight.reset()


@pytest.fixture(autouse=True)
def isolate_the_control_planes_agent_state(tmp_path, monkeypatch):
    """Keep the CA, the enrolment ledger and this machine's identity out of
    the developer's real ``~/.config``.

    ``app.lifespan`` starts the agent transport, and the transport creates a
    certificate authority and an enrolment ledger the first time it runs. Every
    test that builds an app was creating those in the *real* config directory —
    writing a CA key into a developer's home, and then failing to enrol against
    a ledger left behind by a previous test.
    """
    import spark_pulse.app as app_module

    monkeypatch.setattr(app_module, "agent_state_dir", lambda: tmp_path / "_agent")


# ── The node agent's SSH bootstrap ──────────────────────────────────────────
#
# Used by tests/test_agent_bootstrap.py and tests/test_agent_doctor.py. They
# live here rather than in either file because both suites need all three, and
# importing a fixture by name into a module that also takes it as a parameter
# is exactly the shadowing pytest cannot see through.


@pytest.fixture
async def agent_server(tmp_path):
    """A control plane on ephemeral loopback ports, with its own CA."""
    from spark_pulse.agent.hub import AgentHub
    from spark_pulse.agent.server import ControlPlaneServer

    control = ControlPlaneServer(
        directory=tmp_path / "control",
        host="127.0.0.1",
        session_port=0,
        enrollment_port=0,
        hub=AgentHub(cluster_id="bootstrap-tests", epoch=3),
    )
    await control.start()
    try:
        yield control
    finally:
        await control.stop(grace=0)


@pytest.fixture
async def agent_fleet():
    """Simulated nodes, torn down so no agent task outlives a test."""
    from spark_pulse.mock.bootstrap_node import SimulatedFleet

    fleet = SimulatedFleet()
    try:
        yield fleet
    finally:
        await fleet.shutdown()


@pytest.fixture
async def join_agent(agent_server, tmp_path):
    """Enrol stub nodes against ``agent_server`` and hold their sessions.

    These are *stubs*, not agents — see ``tests/agent_stub.py``. The agent that
    ships is one Rust binary and it is driven, as a binary, by
    ``tests/test_agent_rust_interop.py``. What these fixtures exist for is the
    other half: testing the control plane against a counterparty that can
    misbehave, which a correct agent by definition cannot.
    """
    from spark_pulse.mock import agent_node as agent_stub

    stubs: list = []

    async def _join(
        name: str,
        *,
        handler=None,
        node_id: str = "",
        connect: bool = True,
        **kwargs,
    ):
        identity = await agent_stub.enroll(
            agent_server,
            name,
            tmp_path / f"identity-{name}",
            token=agent_server.mint_token(name, node_id=node_id),
        )
        stub = agent_stub.AgentStub(
            identity,
            agent_server.session_target(),
            handler=handler or agent_stub.answer_facts,
            **kwargs,
        )
        stubs.append(stub)
        if connect:
            await stub.connect()
        return stub

    try:
        yield _join
    finally:
        for stub in stubs:
            await stub.close()


@pytest.fixture
async def agent_node(join_agent):
    """One enrolled, connected stub node."""
    return await join_agent("spark-a")


@pytest.fixture(scope="session")
def agent_bundle():
    """The bundle the installer ships: one static binary.

    Built for *this* machine's target, from this machine's binary, rather than
    for the node's. Nothing here executes the payload — the bundle suite
    asserts layout, digest and permissions, and the installer suite ships it to
    a simulated node — so what a real cross-built binary would add is a
    fifteen-minute emulated compile on every run. That the *default* target is
    the node's platform is asserted separately, in
    ``test_the_target_is_the_nodes_platform_not_the_control_planes``, and
    ``scripts/build-agent.sh`` is exercised by its own CI job.

    Session-scoped because it is content-addressed and therefore identical
    every time. Skipped, loudly, when no binary has been built here: the
    alternative is a suite that passes while the thing it ships does not exist.
    """
    import pytest as _pytest

    from spark_pulse.agent.bundle import (
        MissingAgentBinary,
        build_bundle,
        host_binary,
        host_target,
    )

    try:
        return build_bundle(target=host_target(), binary=host_binary())
    except MissingAgentBinary as exc:
        _pytest.skip(str(exc))
