"""Tests for Docker label serialization round-trips.

Verifies that ContainerMetadata can be serialized to Docker labels
and deserialized back without data loss.
"""

import pytest

from spark_pulse.tools.docker import ContainerMetadata


class TestContainerMetadataRoundTrip:
    """Test label serialization/deserialization."""

    def test_basic_metadata_round_trip(self):
        """Test basic fields survive a round trip."""
        original = ContainerMetadata(
            deployment="my-deployment",
            recipe="qwen3.5-122b-fp8",
            image="ghcr.io/eugr/vllm-node:latest",
            mode="solo",
            memory_limit_gb=110,
            shm_size_gb=64,
            privileged=True,
        )

        labels = original.to_labels()
        restored = ContainerMetadata.from_labels(labels)

        assert restored.deployment == "my-deployment"
        assert restored.recipe == "qwen3.5-122b-fp8"
        assert restored.image == "ghcr.io/eugr/vllm-node:latest"
        assert restored.mode == "solo"
        assert restored.memory_limit_gb == 110
        assert restored.shm_size_gb == 64
        assert restored.privileged is True

    def test_optional_fields_defaults(self):
        """Test that optional fields get correct defaults."""
        original = ContainerMetadata(
            deployment="test",
            recipe="test-recipe",
            image="test-image",
        )

        labels = original.to_labels()
        restored = ContainerMetadata.from_labels(labels)

        assert restored.created_at is None
        assert restored.memory_limit_gb is None
        assert restored.shm_size_gb == 64
        assert restored.privileged is True
        assert restored.mode == "solo"

    def test_labels_have_managed_flag(self):
        """Test that all labels have the managed=true flag."""
        metadata = ContainerMetadata(
            deployment="test",
            recipe="test",
            image="test",
        )
        labels = metadata.to_labels()
        assert labels["spark-pulse.managed"] == "true"

    def test_labels_have_version(self):
        """Test that labels include version field."""
        metadata = ContainerMetadata(
            deployment="test",
            recipe="test",
            image="test",
        )
        labels = metadata.to_labels()
        assert labels["spark-pulse.version"] == "1"

    def test_privileged_false(self):
        """Test privileged=False is correctly serialized."""
        metadata = ContainerMetadata(
            deployment="test",
            recipe="test",
            image="test",
            privileged=False,
        )
        labels = metadata.to_labels()
        restored = ContainerMetadata.from_labels(labels)
        assert restored.privileged is False

    def test_empty_labels_returns_defaults(self):
        """Test that empty labels dict returns default values."""
        restored = ContainerMetadata.from_labels({})
        assert restored.deployment == ""
        assert restored.recipe == ""
        assert restored.image == ""
        assert restored.mode == "solo"
        assert restored.shm_size_gb == 64
        assert restored.privileged is True

    def test_none_labels_returns_defaults(self):
        """Test that None labels returns default values."""
        restored = ContainerMetadata.from_labels(None)
        assert restored.deployment == ""
        assert restored.mode == "solo"

    def test_created_at_preserved(self):
        """Test that created_at timestamp is preserved."""
        original = ContainerMetadata(
            deployment="test",
            recipe="test",
            image="test",
            created_at="2026-06-19T10:30:00+00:00",
        )
        labels = original.to_labels()
        restored = ContainerMetadata.from_labels(labels)
        assert restored.created_at == "2026-06-19T10:30:00+00:00"

    def test_memory_limit_zero(self):
        """Test that memory_limit_gb=0 is handled correctly."""
        metadata = ContainerMetadata(
            deployment="test",
            recipe="test",
            image="test",
            memory_limit_gb=0,
        )
        labels = metadata.to_labels()
        # 0 should be serialized as empty string (falsy)
        restored = ContainerMetadata.from_labels(labels)
        # Empty string means None
        assert restored.memory_limit_gb is None

    def test_shm_size_custom(self):
        """Test custom shm_size_gb value."""
        metadata = ContainerMetadata(
            deployment="test",
            recipe="test",
            image="test",
            shm_size_gb=128,
        )
        labels = metadata.to_labels()
        restored = ContainerMetadata.from_labels(labels)
        assert restored.shm_size_gb == 128
