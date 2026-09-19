"""Real cache tools — scanning directories and cleanup."""

import os
import shutil
from pathlib import Path
from typing import Any


def get_cache_dirs() -> list[dict[str, str]]:
    """Return known cache directories.

    Engine runtime caches only — what a model server fills while it serves.
    There were three more once (``wheels``, ``.ccache``, ``uv``), all of them
    build caches belonging to upstream's wheel-building workflow, which
    nothing here has ever run: engines arrive as images and recipes come from
    the bundled set, OCI collections and the operator's own files.
    """
    home = os.path.expanduser("~")
    return [
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
    ]


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
