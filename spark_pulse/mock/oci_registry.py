"""Mock OCI registry provider for development/testing.

Provides simulated OCI operations without requiring actual registry access.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from spark_pulse.tools.oci_registry import (
    CollectionInfo,
    RecipeMeta,
    UpdateInfo,
    RECIPES_DIR,
)


def mock_list_collections(registry_name: str | None = None, version: str | None = None):
    """Mock collection listing."""
    collections = [
        CollectionInfo(
            name="spark-recipes",
            version="1.0.0",
            description="Spark Pulse recipe collection",
            vendor="Kharkevich Engineering Lab",
            license="MIT",
            recipe_count=5,
            digest="sha256:abc123def456",
            registry="ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
        ),
        CollectionInfo(
            name="community-recipes",
            version="0.3.0",
            description="Community-contributed recipes",
            vendor="Community",
            license="Apache-2.0",
            recipe_count=3,
            digest="sha256:789ghi012jkl",
            registry="ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes",
        ),
    ]

    if registry_name:
        collections = [c for c in collections if c.registry == registry_name]
    if version:
        collections = [c for c in collections if c.version == version]

    return collections


def mock_list_collection_recipes(collection_name: str, registry_name: str | None = None, version: str | None = None):
    """Mock recipe listing for a collection."""
    recipes_map = {
        "spark-recipes": [
            {"name": "spark-vllm-7b", "description": "Llama 3.1 8B inference with vLLM", "model": "meta-llama/Llama-3.1-8B-Instruct", "container": "vllm-node", "recipe_version": "1.0.0"},
            {"name": "spark-vllm-13b", "description": "Llama 3.1 70B inference with vLLM", "model": "meta-llama/Llama-3.1-70B-Instruct", "container": "vllm-node", "recipe_version": "1.0.0"},
            {"name": "spark-vllm-20b", "description": "Mistral 22B inference with vLLM", "model": "mistralai/Mistral-22B-Instruct-v0.1", "container": "vllm-node", "recipe_version": "1.0.0"},
            {"name": "spark-vllm-40b", "description": "Mixtral 8x7B inference with vLLM", "model": "mistralai/Mixtral-8x7B-Instruct-v0.1", "container": "vllm-node", "recipe_version": "1.0.0"},
            {"name": "spark-vllm-70b", "description": "Llama 3.1 70B optimized inference", "model": "meta-llama/Llama-3.1-70B-Instruct", "container": "vllm-node", "recipe_version": "1.0.0"},
        ],
        "community-recipes": [
            {"name": "community-llama-3-8b", "description": "Community-tuned Llama 3 8B", "model": "meta-llama/Llama-3-8B", "container": "vllm-node", "recipe_version": "0.3.0"},
            {"name": "community-mixtral-8x7b", "description": "Community-tuned Mixtral 8x7B", "model": "mistralai/Mixtral-8x7B-Instruct-v0.1", "container": "vllm-node", "recipe_version": "0.3.0"},
            {"name": "community-qwen-72b", "description": "Qwen 2.5 72B inference", "model": "Qwen/Qwen2.5-72B-Instruct", "container": "vllm-node", "recipe_version": "0.3.0"},
        ],
    }
    return recipes_map.get(collection_name, [])


def mock_install_collection(name: str, version: str, registry_name: str | None = None):
    """Mock installation — creates sample recipe files and metadata."""
    sample_recipes = {
        "spark-vllm-7b.yaml": {
            "content": """name: spark-vllm-7b
model: meta-llama/Llama-3.1-8B-Instruct
container: vllm-node
description: Llama 3.1 8B inference with vLLM
solo_only: true
cluster_only: false
defaults:
  gpu_count: 1
  max_model_len: 4096
  quantization: null
mods: []
""",
            "digest": "sha256:abc123",
        },
        "spark-vllm-13b.yaml": {
            "content": """name: spark-vllm-13b
model: meta-llama/Llama-3.1-70B-Instruct
container: vllm-node
description: Llama 3.1 70B inference with vLLM
solo_only: false
cluster_only: true
defaults:
  gpu_count: 8
  max_model_len: 8192
  quantization: bitsandbytes
mods: []
""",
            "digest": "sha256:def456",
        },
    }

    RECIPES_DIR.mkdir(parents=True, exist_ok=True)
    installed = []

    for filename, data in sample_recipes.items():
        dest = RECIPES_DIR / filename
        with open(dest, "w") as f:
            f.write(data["content"])

        # Write metadata
        meta_path = RECIPES_DIR / f"{filename}.meta"
        meta = {
            "source": "spark-official",
            "collection": name,
            "version": version,
            "digest": data["digest"],
            "installed_at": "2026-06-15T02:00:00Z",
            "updated_at": "2026-06-15T02:00:00Z",
            "local_changes": False,
        }
        with open(meta_path, "w") as f:
            import yaml
            yaml.dump(meta, f)

        installed.append(filename)

    return installed


def mock_check_updates(collection: str | None = None, registry: str | None = None):
    """Mock update checking."""
    return [
        UpdateInfo(
            collection="spark-recipes",
            current_version="1.0.0",
            latest_version="1.1.0",
            current_digest="sha256:abc123def456",
            latest_digest="sha256:new789xyz",
            local_changes=False,
            added_recipes=["spark-vllm-70b.yaml"],
            modified_recipes=["spark-vllm-7b.yaml"],
        ),
    ]


def mock_list_oci_recipes():
    """Mock listing of OCI-installed recipes."""
    return [
        RecipeMeta(
            name="spark-vllm-7b.yaml",
            source="spark-official",
            collection="spark-recipes",
            version="1.0.0",
            digest="sha256:abc123",
            installed_at="2026-06-15T02:00:00Z",
            updated_at="2026-06-15T02:00:00Z",
            local_changes=False,
        ),
        RecipeMeta(
            name="spark-vllm-13b.yaml",
            source="spark-official",
            collection="spark-recipes",
            version="1.0.0",
            digest="sha256:def456",
            installed_at="2026-06-15T02:00:00Z",
            updated_at="2026-06-15T02:00:00Z",
            local_changes=False,
        ),
    ]
