"""Mock cache tools — plausible cache sizes for a DGX Spark dev setup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_CACHE_ENTRIES = [
    {
        "name": "HF Model Cache",
        "path": "/home/user/.cache/huggingface/hub",
        "size_bytes": 48542741504,
        "file_count": 12,
        "description": "Downloaded HuggingFace models",
    },
    {
        "name": "vLLM Cache",
        "path": "/home/user/.cache/vllm",
        "size_bytes": 2254857830,
        "file_count": 4,
        "description": "vLLM internal cache",
    },
    {
        "name": "FlashInfer Cache",
        "path": "/home/user/.cache/flashinfer",
        "size_bytes": 933232128,
        "file_count": 23,
        "description": "FlashInfer JIT cache",
    },
    {
        "name": "Triton Cache",
        "path": "/home/user/.triton",
        "size_bytes": 1503238553,
        "file_count": 67,
        "description": "Triton compiler cache",
    },
    {
        "name": "CCache",
        "path": "/home/user/.ccache",
        "size_bytes": 13207024435,
        "file_count": 1842,
        "description": "CUDA/C++ compilation cache",
    },
    {
        "name": "uv Pip Cache",
        "path": "/home/user/.cache/uv",
        "size_bytes": 34359738368,
        "file_count": 312,
        "description": "Python package cache",
    },
    {
        "name": "Wheels (spark-vllm)",
        "path": "/tmp/spark-vllm-docker/wheels",
        "size_bytes": 9332321280,
        "file_count": 8,
        "description": "Built/installed wheels",
    },
]

# Expected cache dirs for test compatibility
EXPECTED_CACHE_DIRS = [
    "/home/user/.cache/huggingface",
    "/home/user/.cache/huggingface/datasets",
    "/home/user/.cache/uv",
    "/home/user/.cache/vllm",
    "/root/.cache/huggingface",
    "/root/.cache/huggingface/datasets",
    "/root/.cache/uv",
    "/root/.cache/vllm",
    "/tmp/spark-vllm-docker/build",
    "/tmp/spark-vllm-docker/wheels",
    "/var/lib/docker/overlay2",
]


def get_cache_dirs() -> list[dict[str, str]]:
    """Return expected cache directories to scan (matching real module format)."""
    return [
        {"name": "HF Model Cache", "path": "/home/user/.cache/huggingface/hub", "description": "Downloaded HuggingFace models"},
        {"name": "vLLM Cache", "path": "/home/user/.cache/vllm", "description": "vLLM internal cache"},
        {"name": "FlashInfer Cache", "path": "/home/user/.cache/flashinfer", "description": "FlashInfer JIT cache"},
        {"name": "Triton Cache", "path": "/home/user/.triton", "description": "Triton compiler cache"},
        {"name": "CCache", "path": "/home/user/.ccache", "description": "CUDA/C++ compilation cache"},
        {"name": "uv Pip Cache", "path": "/home/user/.cache/uv", "description": "Python package cache"},
        {"name": "Wheels (spark-vllm)", "path": "/tmp/spark-vllm-docker/wheels", "description": "Built/installed wheels"},
    ]


def scan_dir(path: str) -> dict[str, Any]:
    """Return mock scan results for a directory."""
    p = Path(path)
    if not p.exists():
        return {"size_bytes": 0, "file_count": 0}
    size = 0
    count = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                size += f.stat().st_size
                count += 1
            except OSError:
                pass
    return {"size_bytes": size, "file_count": count}


def list_cache() -> list[dict[str, Any]]:
    """Return mock cache entries."""
    return list(_CACHE_ENTRIES)


def clean_cache(targets: list[str]) -> dict[str, str]:
    """Return mock clean results."""
    results: dict[str, str] = {}
    for t in targets:
        results[t] = f"Mock: cleaned {t}"
    return results
