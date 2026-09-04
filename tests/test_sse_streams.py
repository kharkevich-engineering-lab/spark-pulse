"""The SSE generators must actually run.

Every generator here is reachable only when a browser opens the stream, so a
name that does not resolve, or a blocking call on the event loop, shows up in
production rather than in a unit test. These are the cheap guards.
"""

import asyncio
import inspect
from unittest.mock import patch

import pytest

from spark_pulse import sse


class TestClusterEventStream:
    """The cluster stream imported a function that does not exist."""

    def test_the_cluster_listing_it_imports_resolves(self):
        # `tools.cluster` has no module-level list_clusters; importing it there
        # raised ImportError the moment a client connected.
        from spark_pulse.routers.cluster import list_clusters

        assert callable(list_clusters)

    @pytest.mark.asyncio
    async def test_the_generator_yields_without_raising(self):
        with patch(
            "spark_pulse.routers.cluster.list_clusters", return_value=[]
        ) as listing:
            gen = sse.cluster_events_generator()
            try:
                await asyncio.wait_for(gen.__anext__(), timeout=10)
            except asyncio.TimeoutError:
                # No cluster changed, so no event is due; reaching the timeout
                # means the generator ran rather than blew up on import.
                pass
            finally:
                await gen.aclose()

        assert listing.called, "the generator never reached the cluster listing"


class TestGeneratorsDoNotBlockTheLoop:
    """Blocking work inside an async generator stalls every other stream."""

    @pytest.mark.parametrize(
        "name",
        [
            "cluster_events_generator",
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
