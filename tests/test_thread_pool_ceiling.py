"""The worker-thread ceiling this process runs at.

AnyIO's worker-thread limiter defaults to 40 and says so nowhere, so
exhaustion — a handful of blocking Docker or SSH calls is enough — looked like
the API going quiet for no reason. ``configure_thread_pool`` sets it from
config at startup, and these pin that.

This file also held ``TestRouterReusesOneService``, which proved the docker
router reused one ``DockerService`` instead of building a pool per request.
The router is gone; ``tools.docker._get_service()`` is still the singleton,
and its callers are the node services.
"""

from __future__ import annotations

from unittest.mock import patch

import anyio.to_thread

from spark_pulse.app import configure_thread_pool
from spark_pulse.config import config


class TestThreadPoolCeiling:
    """The worker-thread ceiling is set from config and reported."""

    async def test_the_configured_size_is_applied(self):
        with patch.object(type(config), "thread_pool_size", property(lambda _: 7)):
            applied = await configure_thread_pool()

        assert applied == 7
        assert anyio.to_thread.current_default_thread_limiter().total_tokens == 7

    async def test_startup_pins_the_limiter_to_the_configured_size(self):
        """Whatever anyio's own default is, ours is the one that ends up set."""
        applied = await configure_thread_pool()

        assert applied == config.thread_pool_size
        limiter = anyio.to_thread.current_default_thread_limiter()
        assert limiter.total_tokens == config.thread_pool_size

    def test_the_bundled_default_is_forty(self):
        """The number the code has always run at, now written down."""
        with patch.object(config, "_data", {}):
            assert config.thread_pool_size == 40

    def test_a_nonsense_size_falls_back_to_something_usable(self):
        """Zero threads would wedge every sync endpoint; one is the floor."""
        with patch.object(config, "_data", {"thread_pool_size": 0}):
            assert config.thread_pool_size == 1
