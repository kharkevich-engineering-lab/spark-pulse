"""The SSE generators must actually run.

Every generator here is reachable only when a browser opens the stream, so a
name that does not resolve, or a blocking call on the event loop, shows up in
production rather than in a unit test. These are the cheap guards.
"""

import inspect
import pathlib
from unittest.mock import patch

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
    """Importing a tools submodule directly swaps the mock for the real one.

    `from spark_pulse.tools.deployments import list_deployments` rebinds
    `spark_pulse.tools.deployments` to the real module for the rest of the
    process. One call to the memory endpoint used to switch the whole app off
    the mock store, so deployments created in simulation silently vanished.
    """

    @pytest.mark.parametrize(
        "module", ["spark_pulse/sse.py", "spark_pulse/routers/memory.py"]
    )
    def test_no_direct_submodule_import_of_deployments(self, module):
        source = pathlib.Path(module).read_text()
        assert "from spark_pulse.tools.deployments import" not in source, (
            f"{module} imports the submodule directly, which rebinds it to the "
            "real module and defeats SIMULATION_MODE"
        )

    def test_the_mock_store_survives_a_metrics_collection(self):
        from spark_pulse import tools

        before = tools.deployments.__name__
        with patch.object(
            tools.system, "get_all_memory", return_value={"processes": []}
        ):
            list(sse.metrics_generator().__class__.__mro__)  # generator is lazy
            from spark_pulse.routers import memory

            with patch.object(
                tools.system, "get_all_memory", return_value={"processes": []}
            ):
                memory.get_all_memory()

        assert (
            tools.deployments.__name__ == before
        ), "collecting metrics switched the deployment store"
