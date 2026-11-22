"""Simulation's image sizing: no registry is asked.

A simulated control plane has no network to promise, so every image's size is
unknown — the same answer the real module gives when its registry does not
answer, which the pre-flight reports as such rather than as a number. Tests
that want a size patch :func:`transfer_bytes`.
"""

from __future__ import annotations

from spark_pulse.tools.image_size import manifest_bytes, platform_manifest

__all__ = ["transfer_bytes", "platform_manifest", "manifest_bytes"]


def transfer_bytes(ref: str, platform: str) -> int | None:  # noqa: ARG001
    return None
