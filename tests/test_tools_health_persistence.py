"""Tests for health monitoring persistence."""

from __future__ import annotations

import json

import pytest

from spark_pulse.tools.health import (
    HealthMonitor,
    load_health_tracking,
    save_health_tracking,
)


class TestHealthTrackingPersistence:
    @pytest.fixture
    def tracking_file(self, tmp_path):
        """Override the tracking file path for testing."""
        return tmp_path / "health_tracking.json"

    def test_save_load_round_trip(self, tracking_file, monkeypatch):
        monkeypatch.setattr(
            "spark_pulse.tools.health._HEALTH_TRACKING_FILE",
            tracking_file,
        )

        tracked = {
            "deployments": [{"id": "dep-1", "type": "deployment"}],
            "clusters": [{"name": "cluster-1", "type": "cluster"}],
        }
        save_health_tracking(tracked)

        assert tracking_file.exists()
        loaded = load_health_tracking()
        assert loaded == tracked

    def test_load_nonexistent_file(self, tracking_file, monkeypatch):
        monkeypatch.setattr(
            "spark_pulse.tools.health._HEALTH_TRACKING_FILE",
            tracking_file,
        )
        assert tracking_file.exists() is False

        loaded = load_health_tracking()
        assert loaded == {"deployments": [], "clusters": []}

    def test_load_corrupted_json(self, tracking_file, monkeypatch):
        monkeypatch.setattr(
            "spark_pulse.tools.health._HEALTH_TRACKING_FILE",
            tracking_file,
        )
        tracking_file.write_text("not valid json {{{")

        loaded = load_health_tracking()
        assert loaded == {"deployments": [], "clusters": []}

    def test_save_creates_directory(self, tmp_path, monkeypatch):
        nested = tmp_path / "nested" / "dir" / "tracking.json"
        monkeypatch.setattr(
            "spark_pulse.tools.health._HEALTH_TRACKING_FILE",
            nested,
        )

        save_health_tracking({"deployments": [], "clusters": []})
        assert nested.exists()


class TestHealthMonitorPersistence:
    @pytest.fixture
    def monitor(self):
        return HealthMonitor(check_interval=60.0)

    def test_track_deployment_persists(self, monitor, tmp_path, monkeypatch):
        tracking_file = tmp_path / "health_tracking.json"
        monkeypatch.setattr(
            "spark_pulse.tools.health._HEALTH_TRACKING_FILE",
            tracking_file,
        )

        monitor.track_deployment("dep-1", {"container_name": "test"})
        assert tracking_file.exists()

        data = json.loads(tracking_file.read_text())
        assert len(data["deployments"]) == 1
        assert data["deployments"][0]["id"] == "dep-1"

    def test_track_cluster_persists(self, monitor, tmp_path, monkeypatch):
        tracking_file = tmp_path / "health_tracking.json"
        monkeypatch.setattr(
            "spark_pulse.tools.health._HEALTH_TRACKING_FILE",
            tracking_file,
        )

        monitor.track_cluster("cluster-1", {"head_ip": "10.0.0.1"})
        assert tracking_file.exists()

        data = json.loads(tracking_file.read_text())
        assert len(data["clusters"]) == 1
        assert data["clusters"][0]["name"] == "cluster-1"

    def test_untrack_removes_from_disk(self, monitor, tmp_path, monkeypatch):
        tracking_file = tmp_path / "health_tracking.json"
        monkeypatch.setattr(
            "spark_pulse.tools.health._HEALTH_TRACKING_FILE",
            tracking_file,
        )

        monitor.track_deployment("dep-1", {})
        monitor.untrack("dep-1")

        data = json.loads(tracking_file.read_text())
        assert len(data["deployments"]) == 0

    def test_restore_from_persistence(self, tmp_path, monkeypatch):
        tracking_file = tmp_path / "health_tracking.json"
        monkeypatch.setattr(
            "spark_pulse.tools.health._HEALTH_TRACKING_FILE",
            tracking_file,
        )

        tracked = {
            "deployments": [{"id": "dep-1", "type": "deployment", "info": {}}],
            "clusters": [{"name": "cluster-1", "type": "cluster", "info": {}}],
        }
        save_health_tracking(tracked)

        loaded = HealthMonitor.restore_from_persistence()
        assert loaded == tracked
