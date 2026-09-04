"""Builders for temporary directories shaped like a real HuggingFace hub cache.

Everything these produce lives under a pytest ``tmp_path``. Nothing here reads
or writes the developer's own ``~/.cache/huggingface``, and no test in this
repository may: replication tests delete and truncate files, and a real cache
is hours of downloading.

The layout built here is the one ``huggingface_hub`` writes::

    models--org--name/
      blobs/<sha256 or git sha1>     the bytes
      snapshots/<commit>/<name>      a *relative* symlink into ../../blobs
      refs/main                      the commit hash
      trees/<commit>.json            the manifest, format_version 1

with the manifest's hashes computed the way the hub computes them, so a
verifier checking against it is checking against the real thing.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

#: Files above this size are LFS/Xet on the hub and carry a SHA-256; smaller
#: ones are plain git objects identified by their git SHA-1. The real cutoff is
#: the repo's ``.gitattributes``; what matters for the tests is that both
#: algorithms are exercised, which naming a weights file is enough to do.
LFS_SUFFIXES = (".safetensors", ".bin", ".gguf")


def git_sha1(data: bytes) -> str:
    """git's object id for a blob: sha1 of ``blob <len>\\0<content>``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def repo_dir_name(model_id: str) -> str:
    return "models--" + model_id.replace("/", "--")


def build_cache_entry(
    hub: Path,
    model_id: str,
    commit: str,
    files: dict[str, bytes],
    *,
    ref: str = "main",
    with_manifest: bool = True,
) -> Path:
    """Create one complete cache entry and return its directory.

    The snapshot entries are relative symlinks, exactly as the hub cache writes
    them, because that relativeness is what makes the tree movable — and what a
    copy that does not preserve symlinks destroys.
    """
    repo = hub / repo_dir_name(model_id)
    blobs = repo / "blobs"
    snapshot = repo / "snapshots" / commit
    blobs.mkdir(parents=True, exist_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    (repo / "refs").mkdir(parents=True, exist_ok=True)
    (repo / "refs" / ref).write_text(commit)

    manifest: dict[str, dict[str, object]] = {}
    for name, data in files.items():
        is_lfs = name.endswith(LFS_SUFFIXES)
        blob_name = sha256(data) if is_lfs else git_sha1(data)
        blob_path = blobs / blob_name
        blob_path.write_bytes(data)
        link = snapshot / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(blob_path, link.parent))
        entry: dict[str, object] = {"size": len(data), "blob_id": git_sha1(data)}
        if is_lfs:
            entry["lfs_sha256"] = sha256(data)
            entry["lfs_size"] = len(data)
        manifest[name] = entry

    if with_manifest:
        trees = repo / "trees"
        trees.mkdir(parents=True, exist_ok=True)
        (trees / f"{commit}.json").write_text(
            json.dumps(
                {"format_version": 1, "files": dict(sorted(manifest.items()))}, indent=1
            )
        )
    return repo


#: A small but structurally complete model: a config, a tokenizer and two
#: shards, so the fixture exercises both hash algorithms and a sharded weight
#: set at once.
SAMPLE_FILES: dict[str, bytes] = {
    "config.json": json.dumps(
        {
            "architectures": ["LlamaForCausalLM"],
            "model_type": "llama",
            "torch_dtype": "bfloat16",
        }
    ).encode(),
    "tokenizer.json": b'{"version": "1.0", "model": {"vocab": {}}}',
    "model-00001-of-00002.safetensors": b"\x01" * 4096,
    "model-00002-of-00002.safetensors": b"\x02" * 8192,
}

SAMPLE_COMMIT = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SAMPLE_MODEL = "acme/replicated-7b"


def sample_entry(hub: Path, **kwargs) -> Path:
    """The standard fixture entry: :data:`SAMPLE_MODEL` at :data:`SAMPLE_COMMIT`."""
    return build_cache_entry(
        hub, SAMPLE_MODEL, SAMPLE_COMMIT, dict(SAMPLE_FILES), **kwargs
    )


def blob_for(repo: Path, commit: str, name: str) -> Path:
    """The blob a snapshot entry points at, resolved through its symlink."""
    return (repo / "snapshots" / commit / name).resolve()


def truncate(path: Path, keep: int) -> None:
    """Cut a file down to ``keep`` bytes, as an interrupted transfer would."""
    with open(path, "r+b") as handle:
        handle.truncate(keep)


def corrupt_in_place(path: Path) -> None:
    """Change a file's bytes without changing its size.

    This is the failure a size check cannot see and a hash can, which is the
    whole reason ``--deep`` exists.
    """
    data = bytearray(path.read_bytes())
    data[0] ^= 0xFF
    path.write_bytes(bytes(data))
