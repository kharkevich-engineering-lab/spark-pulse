"""Tests for parallelism parsing and cluster capacity validation."""

from __future__ import annotations


from spark_pulse.tools.parallelism import (
    ClusterCapacity,
    NodeCapacity,
    parse_parallelism,
    validate_cluster_capacity,
)


class TestParseParallelism:
    """Tests for parse_parallelism function."""

    def test_default_values_empty_string(self):
        result = parse_parallelism("")
        assert result == {"tp": 1, "pp": 1, "dp": 1}

    def test_default_values_none(self):
        result = parse_parallelism(None)
        assert result == {"tp": 1, "pp": 1, "dp": 1}

    def test_default_values_path(self):
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("# empty script")
            path = f.name
        try:
            from pathlib import Path

            result = parse_parallelism(Path(path))
            assert result == {"tp": 1, "pp": 1, "dp": 1}
        finally:
            os.unlink(path)

    def test_tensor_parallel_long_flag(self):
        result = parse_parallelism("--tensor-parallel-size=4")
        assert result["tp"] == 4

    def test_tensor_parallel_short_flag(self):
        result = parse_parallelism("-tp 8")
        assert result["tp"] == 8

    def test_pipeline_parallel_long_flag(self):
        result = parse_parallelism("--pipeline-parallel-size=2")
        assert result["pp"] == 2

    def test_pipeline_parallel_short_flag(self):
        result = parse_parallelism("-pp 4")
        assert result["pp"] == 4

    def test_data_parallel_long_flag(self):
        result = parse_parallelism("--data-parallel-size=3")
        assert result["dp"] == 3

    def test_data_parallel_short_flag(self):
        result = parse_parallelism("-dp 2")
        assert result["dp"] == 2

    def test_all_parallelism_flags(self):
        result = parse_parallelism(
            "--tensor-parallel-size=2 --pipeline-parallel-size=4 --data-parallel-size=2"
        )
        assert result["tp"] == 2
        assert result["pp"] == 4
        assert result["dp"] == 2

    def test_mixed_flag_formats(self):
        result = parse_parallelism("-tp=8 --pipeline-parallel-size=2 -dp 1")
        assert result["tp"] == 8
        assert result["pp"] == 2
        assert result["dp"] == 1


class TestNodeCapacity:
    """Tests for NodeCapacity dataclass."""

    def test_max_tp_equals_gpu_count(self):
        node = NodeCapacity(gpu_count=8)
        assert node.max_tp == 8

    def test_max_tp_single_gpu(self):
        node = NodeCapacity(gpu_count=1)
        assert node.max_tp == 1


class TestClusterCapacity:
    """Tests for ClusterCapacity dataclass."""

    def test_total_gpus(self):
        nodes = [
            NodeCapacity(gpu_count=8),
            NodeCapacity(gpu_count=8),
        ]
        capacity = ClusterCapacity(nodes=nodes)
        assert capacity.total_gpus == 16

    def test_max_nodes(self):
        nodes = [
            NodeCapacity(gpu_count=8),
            NodeCapacity(gpu_count=8),
            NodeCapacity(gpu_count=4),
        ]
        capacity = ClusterCapacity(nodes=nodes)
        assert capacity.max_nodes == 3


class TestValidateClusterCapacity:
    """Tests for validate_cluster_capacity function."""

    def test_sufficient_gpus_single_node(self):
        parallelism = {"tp": 8, "pp": 1, "dp": 1}
        nodes = [NodeCapacity(gpu_count=8)]
        capacity = ClusterCapacity(nodes=nodes)

        valid, message = validate_cluster_capacity(parallelism, capacity)
        assert valid is True

    def test_insufficient_total_gpus(self):
        parallelism = {"tp": 8, "pp": 1, "dp": 1}
        nodes = [NodeCapacity(gpu_count=4)]
        capacity = ClusterCapacity(nodes=nodes)

        valid, message = validate_cluster_capacity(parallelism, capacity)
        assert valid is False
        assert "Insufficient GPUs" in message

    def test_insufficient_nodes_for_groups(self):
        parallelism = {"tp": 1, "pp": 1, "dp": 4}
        nodes = [
            NodeCapacity(gpu_count=8),
            NodeCapacity(gpu_count=8),
        ]
        capacity = ClusterCapacity(nodes=nodes)

        valid, message = validate_cluster_capacity(parallelism, capacity)
        assert valid is False
        assert "Insufficient nodes" in message

    def test_tp_exceeds_single_node(self):
        parallelism = {"tp": 8, "pp": 1, "dp": 1}
        nodes = [
            NodeCapacity(gpu_count=4),
            NodeCapacity(gpu_count=4),
        ]
        capacity = ClusterCapacity(nodes=nodes)

        valid, message = validate_cluster_capacity(parallelism, capacity)
        assert valid is False

    def test_multi_node_tp_pp(self):
        """tp=2, pp=4 needs 8 GPUs as 4 nodes×2."""
        parallelism = {"tp": 2, "pp": 4, "dp": 1}
        nodes = [NodeCapacity(gpu_count=2)] * 4
        capacity = ClusterCapacity(nodes=nodes)

        valid, message = validate_cluster_capacity(parallelism, capacity)
        assert valid is True

    def test_data_parallel_nodes(self):
        """dp=4 needs 4 nodes with ≥1 GPU each."""
        parallelism = {"tp": 1, "pp": 1, "dp": 4}
        nodes = [NodeCapacity(gpu_count=1)] * 4
        capacity = ClusterCapacity(nodes=nodes)

        valid, message = validate_cluster_capacity(parallelism, capacity)
        assert valid is True

    def test_message_includes_capacity_details(self):
        parallelism = {"tp": 2, "pp": 2, "dp": 1}
        nodes = [NodeCapacity(gpu_count=8), NodeCapacity(gpu_count=8)]
        capacity = ClusterCapacity(nodes=nodes)

        valid, message = validate_cluster_capacity(parallelism, capacity)
        assert valid is True
        assert "16 GPUs" in message
        assert "2 nodes" in message
