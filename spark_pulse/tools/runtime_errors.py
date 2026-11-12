"""The error a run raises when it cannot be planned or started.

Its own module so the helpers split out of :mod:`native_runtime` (placement
needs it for an exhausted port range) can raise the very class callers catch,
without importing the runtime they were split from.
"""

from __future__ import annotations


class NativeRuntimeError(RuntimeError):
    """A native deployment could not be planned or started."""
