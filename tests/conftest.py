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
