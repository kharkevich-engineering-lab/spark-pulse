"""The SSE generators must actually run.

Every generator here is reachable only when a browser opens the stream, so a
name that does not resolve, or a blocking call on the event loop, shows up in
production rather than in a unit test. These are the cheap guards.
"""

import inspect
import pathlib

import pytest

from spark_pulse import sse


class TestGeneratorsDoNotBlockTheLoop:
    """Blocking work inside an async generator stalls every other stream."""

    @pytest.mark.parametrize(
        "name",
        [
            "metrics_generator",
            "_native_log_generator",
        ],
    )
    def test_generator_offloads_blocking_calls(self, name):
        source = inspect.getsource(getattr(sse, name))
        # Docker, SSH and nvidia-smi calls are synchronous; on the event loop
        # they block every other connected stream in the process.
        assert (
            "run_in_threadpool" in source
        ), f"{name} calls blocking code directly on the event loop"


class TestSimulationSwitchSurvives:
    """Importing a switched tools submodule directly swaps the mock for the real one.

    `from spark_pulse.tools.node_service import service_for` rebinds
    `spark_pulse.tools.node_service` to the real module for the rest of the
    process. One call to the memory endpoint used to switch the whole app off
    the mock store this way, so what simulation had created silently vanished.
    The metrics path reaches every node through the resolver, so that is the
    module that must not be imported as a submodule.
    """

    @pytest.mark.parametrize(
        "module", ["spark_pulse/sse.py", "spark_pulse/routers/memory.py"]
    )
    def test_no_direct_submodule_import_of_switched_tools(self, module):
        source = pathlib.Path(module).read_text()
        for switched in ("node_service", "docker", "recipes", "native_runtime"):
            assert f"from spark_pulse.tools.{switched} import" not in source, (
                f"{module} imports the {switched} submodule directly, which "
                "rebinds it to the real module and defeats SIMULATION_MODE"
            )

    def test_the_mock_tools_survive_a_metrics_collection(self):
        from spark_pulse import tools
        from spark_pulse.routers import memory

        before = tools.node_service.__name__

        memory.get_all_memory()

        assert (
            tools.node_service.__name__ == before
        ), "collecting metrics switched a simulated tool for the real one"
