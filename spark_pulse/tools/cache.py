"""Real cache tools — scanning directories and cleanup."""

import os
import shutil
from pathlib import Path
from typing import Any

from spark_pulse.config import config

WHEELS_CACHE_NAME = "Wheels (spark-vllm)"


def get_cache_dirs() -> list[dict[str, str]]:
    """Return known cache directories.

    The wheels directory belongs to a spark-vllm-docker checkout, so it is
    listed only when there is one; a path pointing nowhere would otherwise show
    up as a permanently empty cache an operator cannot clean.
    """
    home = os.path.expanduser("~")
    dirs = [
        {
            "name": "HF Model Cache",
            "path": f"{home}/.cache/huggingface/hub",
            "description": "Downloaded HuggingFace models",
        },
        {
            "name": "vLLM Cache",
            "path": f"{home}/.cache/vllm",
            "description": "vLLM internal cache",
        },
        {
            "name": "FlashInfer Cache",
            "path": f"{home}/.cache/flashinfer",
            "description": "FlashInfer JIT cache",
        },
        {
            "name": "Triton Cache",
            "path": f"{home}/.triton",
            "description": "Triton compiler cache",
        },
        {
            "name": "CCache",
            "path": f"{home}/.ccache",
            "description": "CUDA/C++ compilation cache",
        },
        {
            "name": "uv Pip Cache",
            "path": f"{home}/.cache/uv",
            "description": "Python package cache",
        },
    ]
    checkout = config.spark_vllm_dir
    if checkout is not None:
        dirs.append(
            {
                "name": WHEELS_CACHE_NAME,
                "path": str(checkout / "wheels"),
                "description": "Built/installed wheels",
            }
        )
    return dirs


def scan_dir(path: str) -> dict[str, Any]:
    """Scan a directory and return size + file count."""
    p = Path(path)
    if not p.exists():
        return {
            "name": Path(path).name,
            "path": path,
            "size_bytes": 0,
            "file_count": 0,
            "description": "",
        }
    total_size = 0
    file_count = 0
    try:
        for item in p.rglob("*"):
            if item.is_file():
                try:
                    total_size += item.stat().st_size
                    file_count += 1
                except OSError:
                    pass
    except OSError:
        pass
    return {
        "name": p.name,
        "path": path,
        "size_bytes": total_size,
        "file_count": file_count,
        "description": "",
    }


def list_cache() -> list[dict[str, Any]]:
    """List all cache directories with scanned sizes."""
    dirs = get_cache_dirs()
    entries = [scan_dir(d["path"]) for d in dirs]
    for entry, orig in zip(entries, dirs):
        entry["description"] = orig["description"]
    return entries


def clean_cache(targets: list[str]) -> dict[str, str]:
    """Clean specified cache directories. Returns status per target."""
    all_dirs = {d["name"]: d["path"] for d in get_cache_dirs()}
    if "all" in targets:
        targets = list(all_dirs.keys())

    results: dict[str, str] = {}
    for target in targets:
        if target in all_dirs:
            p = Path(all_dirs[target])
            if not p.exists():
                results[target] = "Cache directory does not exist"
            else:
                try:
                    for item in p.iterdir():
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink(missing_ok=True)
                    results[target] = f"Cleaned {p}"
                except OSError as e:
                    results[target] = f"Error: {e}"
        else:
            results[target] = f"Unknown cache: {target}"
    return results
