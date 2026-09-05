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


class JoinedAgent:
    """One enrolled agent, its task, and the Docker it speaks to."""

    def __init__(self, agent, task, docker):
        self.agent = agent
        self.task = task
        self.docker = docker

    @property
    def node_id(self) -> str:
        return self.agent.node_id

    async def close(self) -> None:
        await self.agent.stop()
        self.task.cancel()
        try:
            await self.task
        except BaseException:
            pass


@pytest.fixture
async def join_agent(agent_server, tmp_path):
    """Enrol nodes against ``agent_server`` and hold their sessions.

    Each node gets its **own** ``MockDockerService``. That is the point rather
    than tidiness: when every node reads one daemon, an answer for the wrong
    node looks exactly like the right answer, which is how thirteen call sites
    queried the control node while claiming to reach a worker.
    """
    import asyncio

    from spark_pulse.agent.executor import LocalExecutor
    from spark_pulse.agent.node_agent import NodeAgent, enroll
    from spark_pulse.mock.docker import MockDockerClient, MockDockerService

    joined: list[JoinedAgent] = []

    async def _join(
        name: str, *, docker=None, node_id: str = "", heartbeat: float = 0.2
    ):
        docker = docker or MockDockerService(MockDockerClient())
        identity = await enroll(
            agent_server.enrollment_target(),
            agent_server.mint_token(name, node_id=node_id),
            trust_bundle_pem=agent_server.trust_bundle_pem,
            trust_bundle_pin=agent_server.trust_bundle_pin,
            directory=tmp_path / f"identity-{name}",
            requested_name=name,
            docker_service=docker,
        )
        agent = NodeAgent(
            identity,
            agent_server.session_target(),
            executor=LocalExecutor(docker),
            heartbeat_interval=heartbeat,
        )
        task = asyncio.create_task(agent.run_forever(), name=f"agent-{name}")
        await agent.wait_connected(10)
        node = JoinedAgent(agent, task, docker)
        joined.append(node)
        return node

    try:
        yield _join
    finally:
        for node in joined:
            await node.close()


@pytest.fixture
async def agent_node(join_agent):
    """One enrolled, connected agent."""
    return await join_agent("spark-a")


@pytest.fixture
def agent_bundle():
    """An agent bundle with no vendored runtime.

    The "node" in these tests is this interpreter, which already has grpcio:
    what is under test is the shipping and unpacking, not the copying of a
    hundred megabytes of shared objects into a temporary directory.
    """
    from spark_pulse.agent.bundle import build_bundle

    return build_bundle(include_runtime=False)
