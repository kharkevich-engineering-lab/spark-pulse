"""Whether llama-benchy is installed is a plain import (the lab's case).

Kept apart from the router tests, whose autouse fixture reports the tool as
installed: here the real check is the subject.
"""

from __future__ import annotations

import builtins
import importlib


def test_a_missing_llama_benchy_is_reported_with_the_install_command(monkeypatch):
    real = importlib.import_module("spark_pulse.tools.benchmarking")
    original = builtins.__import__

    def no_benchy(name, *args, **kwargs):
        if name == "llama_benchy":
            raise ImportError(name)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_benchy)
    status = real.tool_status()

    assert status == {
        "available": False,
        "reason": "llama-benchy is not installed on the control plane",
        "install": "pip install 'spark-pulse[benchmarking]'",
    }


def test_simulation_is_never_missing_it():
    mock = importlib.import_module("spark_pulse.mock.benchmarking")
    assert mock.tool_status()["available"] is True
