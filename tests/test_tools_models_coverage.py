"""The model catalogue's edge cases: bad metadata, bad disks, bad nodes.

``tests/test_tools_models.py`` covers the happy paths — a cache that is
well-formed, a download that completes, a node that answers.  This file covers
what the module actually spends its code on: a ``config.json`` that is not
JSON, a snapshot directory that cannot be read, a download cancelled between
the queue and the thread, an ``HF_ENDPOINT`` that has to be put back, a node
that verifies and then fails to rename.

Nothing here reaches the network or the developer's own ``~/.cache``: the hub
is a ``tmp_path``, ``huggingface_hub`` is mocked at its own boundary, and SSH
is a scripted double.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from hub_cache_fixtures import SAMPLE_COMMIT, SAMPLE_FILES, SAMPLE_MODEL, sample_entry

# pytest-env forces SIMULATION_MODE=1, so ``from spark_pulse.tools import
# models`` hands back the mock.  ``spark_pulse.mock.models`` imports the real
# module during package init, so it is already in sys.modules and this import
# neither reloads it nor rebinds the tools-package attribute — the simulation
# switch survives untouched.  (Contrast ``tests/test_tools_cache_scan.py``,
# whose module has no such importer and must save and restore the attribute.)
models_tool = importlib.import_module("spark_pulse.tools.models")
hub_cache = importlib.import_module("spark_pulse.tools.hub_cache")

from spark_pulse import tools as tools_pkg  # noqa: E402
from spark_pulse.tools.ssh import (  # noqa: E402
    SSHClient,
    SSHError,
    SSHErrorType,
    SSHResult,
)


# ── Shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_module_state():
    """No job, and no registered loop, may outlive the test that made it."""
    loop = models_tool._loop
    yield
    models_tool._jobs.clear()
    models_tool._cancelled.clear()
    models_tool._loop = loop


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """An empty hub cache under HF_HOME. Never the real one."""
    home = tmp_path / "hf"
    hub_path = home / "hub"
    hub_path.mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(home))
    return hub_path


class _FakeBroadcaster:
    """The SSE broadcaster, minus the SSE."""

    def __init__(self):
        self.events: list[object] = []

    async def emit(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def broadcaster(monkeypatch):
    import spark_pulse.sse as sse

    fake = _FakeBroadcaster()
    monkeypatch.setattr(sse, "_get_event_broadcaster", lambda: fake)
    return fake


# ── Event publishing ─────────────────────────────────────────────────────────


class TestPublishEvent:
    """Download jobs run on worker threads; the broadcaster is asyncio."""

    def test_with_no_listening_loop_a_publish_is_dropped_not_raised(self, broadcaster):
        """A download must not fail because nobody has opened /sse/models."""
        models_tool.register_event_loop(None)

        models_tool.publish_event(models_tool.EVENT_STARTED, "acme/x", {"id": "j1"})

        assert broadcaster.events == []

    def test_a_stopped_loop_is_not_published_to(self, broadcaster):
        """A client that disconnected leaves a loop behind; it is not usable."""
        closed = asyncio.new_event_loop()
        closed.close()
        models_tool.register_event_loop(closed)

        models_tool.publish_event(models_tool.EVENT_STARTED, "acme/x", {"id": "j1"})

        assert broadcaster.events == []

    async def test_a_publish_from_the_loop_itself_is_scheduled_on_it(self, broadcaster):
        models_tool.publish_event(
            models_tool.EVENT_PROGRESS, "acme/x", {"bytes_done": 7}
        )

        await asyncio.sleep(0)

        assert len(broadcaster.events) == 1
        event = broadcaster.events[0]
        assert event.resource == "acme/x"
        assert event.resource_type == "model"
        assert event.event_type is models_tool.EVENT_PROGRESS
        assert event.metadata == {"bytes_done": 7}

    async def test_a_publish_from_a_worker_thread_reaches_the_registered_loop(
        self, broadcaster
    ):
        """The download thread has no loop of its own — sse.py registers one."""
        models_tool.register_event_loop(asyncio.get_running_loop())

        await asyncio.to_thread(
            models_tool.publish_event,
            models_tool.EVENT_COMPLETED,
            "acme/x",
            {"id": "j1"},
        )
        for _ in range(200):
            if broadcaster.events:
                break
            await asyncio.sleep(0.01)

        assert [e.resource for e in broadcaster.events] == ["acme/x"]


# ── Sources and secrets ──────────────────────────────────────────────────────


class TestSourceToken:
    def test_a_source_with_no_secret_named_gets_no_token(self):
        assert models_tool._source_token({"name": "hf"}) == ""
        assert models_tool._source_token({"token_secret": ""}) == ""

    def test_the_hf_token_comes_from_its_own_config_property(self, monkeypatch):
        monkeypatch.setenv("HF_TOKEN", "hf_from_env")

        assert models_tool._source_token({"token_secret": "hf_token"}) == "hf_from_env"

    def test_any_other_secret_is_looked_up_in_the_secrets_store(self, monkeypatch):
        asked: list[str] = []
        # On the class, not the instance: an instance attribute would outlive
        # the undo and shadow every later patch of the same method.
        monkeypatch.setattr(
            type(models_tool.config),
            "get_secret",
            lambda self, key: asked.append(key) or "mirror-token",
        )

        assert models_tool._source_token({"token_secret": "mirror_token"}) == (
            "mirror-token"
        )
        assert asked == ["mirror_token"]


# ── Measuring a directory ────────────────────────────────────────────────────


class TestDirStats:
    def test_a_blob_reached_through_a_symlink_is_counted_once(self, tmp_path):
        """The hub cache is symlinks into blobs; double counting inflates
        every size the Models page shows."""
        blobs = tmp_path / "blobs"
        blobs.mkdir()
        blob = blobs / "abc123"
        blob.write_bytes(b"x" * 500)
        snapshot = tmp_path / "snapshots" / "commit"
        snapshot.mkdir(parents=True)
        (snapshot / "model.safetensors").symlink_to(os.path.relpath(blob, snapshot))

        size, mtime = models_tool._dir_stats(tmp_path)

        assert size == 500
        assert mtime > 0

    def test_a_file_that_vanishes_between_the_walk_and_the_stat_is_skipped(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "here").write_bytes(b"a" * 10)
        (tmp_path / "gone").write_bytes(b"b" * 90)
        real_stat = models_tool.Path.stat

        def flaky_stat(self, *args, **kwargs):
            if self.name == "gone":
                raise OSError("no such file")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(models_tool.Path, "stat", flaky_stat)

        assert models_tool._dir_stats(tmp_path)[0] == 10

    def test_a_directory_that_cannot_be_walked_measures_zero(
        self, tmp_path, monkeypatch
    ):
        def denied(*_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(models_tool.os, "walk", denied)

        assert models_tool._dir_stats(tmp_path) == (0, 0.0)


# ── Reading a model's config.json ────────────────────────────────────────────


class TestConfigSummary:
    def test_a_missing_config_json_summarises_to_nothing(self, tmp_path):
        assert models_tool._config_summary(tmp_path) is None

    def test_a_config_json_that_is_not_json_is_not_an_error(self, tmp_path):
        (tmp_path / "config.json").write_text("{ this was truncated mid-download")

        assert models_tool._config_summary(tmp_path) is None

    def test_a_config_json_that_is_not_an_object_is_rejected(self, tmp_path):
        (tmp_path / "config.json").write_text("[1, 2, 3]")

        assert models_tool._config_summary(tmp_path) is None

    def test_a_config_json_that_cannot_be_opened_is_not_an_error(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "config.json").write_text("{}")

        def denied(*_args, **_kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr("builtins.open", denied)

        assert models_tool._config_summary(tmp_path) is None

    def test_a_quantization_config_that_is_not_an_object_reports_no_quantization(
        self, tmp_path
    ):
        (tmp_path / "config.json").write_text(
            json.dumps({"model_type": "llama", "quantization_config": "awq"})
        )

        summary = models_tool._config_summary(tmp_path)

        assert summary["quantization"] == []
        assert summary["quantization_method"] is None
        assert summary["architectures"] == []


# ── Which recipes point at which model ───────────────────────────────────────


class _StubRecipes:
    def __init__(self, recipes):
        self._recipes = recipes

    def list_recipes(self):
        if isinstance(self._recipes, Exception):
            raise self._recipes
        return self._recipes


class TestRecipeIndex:
    def test_recipes_are_indexed_by_lowercased_model_id(self, monkeypatch):
        monkeypatch.setattr(
            tools_pkg,
            "recipes",
            _StubRecipes(
                [
                    {"id": "a.yaml", "model": "Acme/Plain-7B"},
                    {"id": "b.yaml", "model": "acme/plain-7b"},
                    {"name": "c", "model": "acme/other"},
                ]
            ),
        )

        assert models_tool._recipe_index() == {
            "acme/plain-7b": ["a.yaml", "b.yaml"],
            "acme/other": ["c"],
        }

    def test_recipes_without_a_usable_model_are_skipped(self, monkeypatch):
        monkeypatch.setattr(
            tools_pkg,
            "recipes",
            _StubRecipes(
                [
                    {"id": "a.yaml", "model": "unknown"},
                    {"id": "b.yaml", "model": "   "},
                    {"id": "c.yaml"},
                    {"id": "d.yaml", "model": "acme/real"},
                ]
            ),
        )

        assert models_tool._recipe_index() == {"acme/real": ["d.yaml"]}

    def test_a_broken_recipe_directory_leaves_the_catalogue_unannotated(
        self, monkeypatch
    ):
        """The catalogue is about the cache; recipes are only an annotation."""
        monkeypatch.setattr(
            tools_pkg, "recipes", _StubRecipes(RuntimeError("no checkout"))
        )

        assert models_tool._recipe_index() == {}

    def test_a_source_that_lists_no_recipes_indexes_nothing(self, monkeypatch):
        monkeypatch.setattr(tools_pkg, "recipes", _StubRecipes(None))

        assert models_tool._recipe_index() == {}


# ── Listing a cache entry's revisions ────────────────────────────────────────


class TestRevisions:
    def test_a_repo_with_no_snapshots_directory_has_no_revisions(self, tmp_path):
        (tmp_path / "blobs").mkdir()

        assert models_tool._revisions(tmp_path) == []

    def test_a_stray_file_among_the_snapshots_is_not_a_revision(self, tmp_path):
        snapshots = tmp_path / "snapshots"
        snapshots.mkdir()
        (snapshots / "aaaa").mkdir()
        (snapshots / "aaaa" / "config.json").write_text("{}")
        (snapshots / ".DS_Store").write_bytes(b"junk")

        assert [r["revision"] for r in models_tool._revisions(tmp_path)] == ["aaaa"]

    def test_a_ref_that_cannot_be_read_leaves_the_revision_unlabelled(
        self, tmp_path, monkeypatch
    ):
        snapshots = tmp_path / "snapshots" / "aaaa"
        snapshots.mkdir(parents=True)
        (snapshots / "weights.bin").write_bytes(b"x" * 8)
        refs = tmp_path / "refs"
        refs.mkdir()
        (refs / "main").write_text("aaaa")

        def denied(self, *args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(models_tool.Path, "read_text", denied)

        revisions = models_tool._revisions(tmp_path)

        assert [r["revision"] for r in revisions] == ["aaaa"]
        assert revisions[0]["refs"] == []
        assert revisions[0]["size_bytes"] == 8

    def test_a_directory_among_the_refs_is_not_a_ref(self, tmp_path):
        snapshots = tmp_path / "snapshots" / "aaaa"
        snapshots.mkdir(parents=True)
        (tmp_path / "refs" / "pr").mkdir(parents=True)

        assert models_tool._revisions(tmp_path)[0]["refs"] == []


# ── local_path sources ───────────────────────────────────────────────────────


class TestLocalSourceModels:
    def test_a_source_path_that_does_not_exist_lists_nothing(self, tmp_path):
        source = {"name": "vault", "type": "local_path", "path": str(tmp_path / "no")}

        assert models_tool._local_source_models(source) == []

    def test_a_source_path_that_is_itself_a_model_is_the_only_entry(self, tmp_path):
        root = tmp_path / "mistral-7b"
        root.mkdir()
        (root / "config.json").write_text(json.dumps({"model_type": "mistral"}))
        (root / "model.safetensors").write_bytes(b"z" * 64)

        entries = models_tool._local_source_models(
            {"name": "vault", "type": "local_path", "path": str(root)}
        )

        assert [e["id"] for e in entries] == ["mistral-7b"]
        assert entries[0]["source"] == "vault"
        assert entries[0]["source_type"] == "local_path"
        assert entries[0]["size_bytes"] >= 64
        assert entries[0]["config"]["model_type"] == "mistral"
        assert entries[0]["last_modified"]

    def test_models_are_found_one_and_two_levels_down(self, tmp_path):
        (tmp_path / "solo").mkdir()
        (tmp_path / "solo" / "config.json").write_text("{}")
        (tmp_path / "org" / "nested").mkdir(parents=True)
        (tmp_path / "org" / "nested" / "config.json").write_text("{}")
        (tmp_path / "junk").mkdir()
        (tmp_path / "loose.txt").write_text("not a model")

        entries = models_tool._local_source_models(
            {"name": "vault", "type": "local_path", "path": str(tmp_path)}
        )

        assert sorted(e["id"] for e in entries) == [
            str(Path("org") / "nested"),
            "solo",
        ]

    def test_the_source_path_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "models" / "tiny").mkdir(parents=True)
        (tmp_path / "models" / "tiny" / "config.json").write_text("{}")

        entries = models_tool._local_source_models(
            {"name": "vault", "type": "local_path", "path": "~/models"}
        )

        assert [e["id"] for e in entries] == ["tiny"]


# ── Estimating a download ────────────────────────────────────────────────────


class _Sibling:
    def __init__(self, rfilename, size=None):
        self.rfilename = rfilename
        self.size = size


class _Info:
    def __init__(self, siblings):
        self.siblings = siblings


class _FakeHfApi:
    """Stands in for huggingface_hub.HfApi at the library boundary."""

    instances: list[_FakeHfApi] = []

    def __init__(self, endpoint=None, token=None):
        self.endpoint = endpoint
        self.token = token
        self.calls: list[tuple] = []
        _FakeHfApi.instances.append(self)

    def model_info(self, model, revision=None, files_metadata=False):
        self.calls.append((model, revision, files_metadata))
        return _Info(
            [
                _Sibling("config.json", 1000),
                _Sibling("model-00001-of-00002.safetensors", 4_000_000),
                _Sibling("model-00002-of-00002.safetensors", 8_000_000),
                _Sibling("README.md"),  # the hub gave no size for this one
            ]
        )


@pytest.fixture
def fake_hf_api(monkeypatch):
    _FakeHfApi.instances = []
    monkeypatch.setattr("huggingface_hub.HfApi", _FakeHfApi)
    return _FakeHfApi


class TestEstimateSize:
    SOURCE = {
        "name": "mirror",
        "type": "hf_hub",
        "endpoint": "http://mirror.local",
        "token_secret": "",
    }

    def test_the_estimate_is_the_sum_of_the_files_the_hub_reports(self, fake_hf_api):
        assert models_tool.estimate_size("acme/plain-7b", self.SOURCE) == 12_001_000

    def test_a_file_the_hub_gives_no_size_for_counts_as_nothing(self, fake_hf_api):
        """Better a low estimate than a crash on incomplete metadata."""
        total = models_tool.estimate_size("acme/plain-7b", self.SOURCE)

        assert total == 1000 + 4_000_000 + 8_000_000

    def test_allow_patterns_narrow_the_estimate_to_what_will_be_fetched(
        self, fake_hf_api
    ):
        total = models_tool.estimate_size(
            "acme/plain-7b", self.SOURCE, allow_patterns=["*.safetensors"]
        )

        assert total == 12_000_000

    def test_the_source_endpoint_token_and_revision_are_all_passed_on(
        self, fake_hf_api, monkeypatch
    ):
        monkeypatch.setattr(
            type(models_tool.config), "get_secret", lambda self, key: "sekrit"
        )
        source = dict(self.SOURCE, token_secret="mirror_token")

        models_tool.estimate_size("acme/plain-7b", source, revision="v2")

        api = fake_hf_api.instances[-1]
        assert api.endpoint == "http://mirror.local"
        assert api.token == "sekrit"
        assert api.calls == [("acme/plain-7b", "v2", True)]

    def test_a_hub_that_will_not_answer_estimates_nothing(self, monkeypatch):
        """0 means "unknown", and the disk-space guard skips an unknown."""

        class _Broken:
            def __init__(self, *a, **k):
                raise OSError("dns failure")

        monkeypatch.setattr("huggingface_hub.HfApi", _Broken)

        assert models_tool.estimate_size("acme/plain-7b", self.SOURCE) == 0

    def test_a_model_the_hub_lists_no_files_for_estimates_nothing(self, monkeypatch):
        class _Empty:
            def __init__(self, *a, **k):
                pass

            def model_info(self, *a, **k):
                return object()

        monkeypatch.setattr("huggingface_hub.HfApi", _Empty)

        assert models_tool.estimate_size("acme/plain-7b", self.SOURCE) == 0


# ── The disk-space guard ─────────────────────────────────────────────────────


class _Usage:
    """What ``shutil.disk_usage`` returns, minus the filesystem."""

    def __init__(self, free):
        self.free = free
        self.total = free
        self.used = 0


class TestCheckDiskSpace:
    def test_the_probe_walks_up_to_a_directory_that_exists(self, tmp_path):
        """The hub directory may not exist yet; its filesystem still does."""
        probed: list[str] = []

        def usage(path):
            probed.append(path)
            return _Usage(free=10**12)

        with patch("shutil.disk_usage", side_effect=usage):
            models_tool.check_disk_space(1000, tmp_path / "hf" / "hub" / "not" / "yet")

        assert probed == [str(tmp_path)]

    def test_a_filesystem_that_will_not_report_free_space_does_not_block(
        self, tmp_path
    ):
        with patch("shutil.disk_usage", side_effect=OSError("stale nfs handle")):
            models_tool.check_disk_space(10**15, tmp_path)

    def test_the_message_names_the_target_and_both_sizes(self, tmp_path):
        with patch("shutil.disk_usage", return_value=_Usage(free=2 * 10**9)):
            with pytest.raises(ValueError) as excinfo:
                models_tool.check_disk_space(9 * 10**9, tmp_path)

        message = str(excinfo.value)
        assert "2.0 GB available" in message
        assert "9.0 GB required" in message
        assert str(tmp_path) in message


# ── Download jobs: the parts that are not the happy path ─────────────────────


def _seed_job(**fields) -> str:
    job = {
        "id": "job1",
        "model": "acme/plain-7b",
        "status": "queued",
        "bytes_done": 0,
        "bytes_total": 0,
        "created_at": models_tool._now(),
        "revision": None,
        "allow_patterns": None,
        "path": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
    }
    job.update(fields)
    models_tool._jobs[job["id"]] = job
    return job["id"]


class TestSetJob:
    def test_updating_a_job_that_no_longer_exists_returns_nothing(self):
        """clear_finished_downloads can drop a job while a thread still holds
        its id; the thread must not raise."""
        assert models_tool._set_job("vanished", status="completed") is None


class TestProgressMonitor:
    """The monitor is what makes a multi-hour download show a moving bar."""

    def test_progress_is_the_repo_directory_growing_on_disk(self, hub):
        repo = hub / "models--acme--plain-7b"
        repo.mkdir()
        (repo / "blob").write_bytes(b"x" * 4096)
        job_id = _seed_job(status="running")
        stop = threading.Event()
        published: list[dict] = []

        def record(event, job):
            published.append((event, dict(job)))
            stop.set()

        with (
            patch.object(models_tool, "_publish_job", side_effect=record),
            patch.object(models_tool, "_PROGRESS_INTERVAL", 0.01),
        ):
            models_tool._progress_monitor(job_id, repo, stop)

        assert [e for e, _ in published] == [models_tool.EVENT_PROGRESS]
        assert published[0][1]["bytes_done"] == 4096
        assert models_tool.get_download(job_id)["bytes_done"] == 4096

    def test_the_monitor_stops_once_the_job_leaves_the_running_state(self, hub):
        """No progress events after the completion event."""
        repo = hub / "models--acme--plain-7b"
        repo.mkdir()
        job_id = _seed_job(status="completed")
        stop = threading.Event()

        with (
            patch.object(models_tool, "_publish_job") as publish,
            patch.object(models_tool, "_PROGRESS_INTERVAL", 0.01),
        ):
            models_tool._progress_monitor(job_id, repo, stop)

        publish.assert_not_called()

    def test_the_monitor_stops_when_the_job_is_cleared_away(self, hub):
        repo = hub / "models--acme--plain-7b"
        repo.mkdir()
        stop = threading.Event()

        with (
            patch.object(models_tool, "_publish_job") as publish,
            patch.object(models_tool, "_PROGRESS_INTERVAL", 0.01),
        ):
            models_tool._progress_monitor("never-existed", repo, stop)

        publish.assert_not_called()


class TestRunDownload:
    """``_run_download`` is the body of the download thread, called directly."""

    SOURCE = {"name": "hf", "type": "hf_hub", "endpoint": "", "token_secret": ""}

    def test_a_job_that_was_cleared_before_the_thread_ran_does_nothing(self, hub):
        with patch("huggingface_hub.snapshot_download") as download:
            models_tool._run_download("vanished", self.SOURCE)

        download.assert_not_called()

    def test_a_job_cancelled_before_the_thread_ran_never_downloads(self, hub):
        job_id = _seed_job(status="queued")
        models_tool._cancelled.add(job_id)
        events: list = []

        with (
            patch("huggingface_hub.snapshot_download") as download,
            patch.object(
                models_tool, "_publish_job", side_effect=lambda e, j: events.append(e)
            ),
        ):
            models_tool._run_download(job_id, self.SOURCE)

        download.assert_not_called()
        assert events == [models_tool.EVENT_CANCELLED]
        job = models_tool.get_download(job_id)
        assert job["status"] == "cancelled"
        assert job["finished_at"]

    def test_a_download_that_dies_because_it_was_cancelled_is_not_a_failure(self, hub):
        """snapshot_download raises when its temp files are pulled away; the
        job the operator cancelled must not be reported as an error."""
        job_id = _seed_job(status="queued")

        def cancelled_mid_flight(**_kwargs):
            models_tool._cancelled.add(job_id)
            raise OSError("Interrupted system call")

        with (
            patch(
                "huggingface_hub.snapshot_download", side_effect=cancelled_mid_flight
            ),
            patch.object(models_tool, "_publish_job"),
        ):
            models_tool._run_download(job_id, self.SOURCE)

        job = models_tool.get_download(job_id)
        assert job["status"] == "cancelled"
        assert job["error"] is None

    def test_a_failure_with_no_message_still_names_the_exception(self, hub):
        job_id = _seed_job(status="queued")

        with (
            patch("huggingface_hub.snapshot_download", side_effect=KeyboardInterrupt()),
            patch.object(models_tool, "_publish_job"),
        ):
            models_tool._run_download(job_id, self.SOURCE)

        assert models_tool.get_download(job_id)["error"] == "KeyboardInterrupt"

    def test_a_pre_existing_hf_endpoint_is_restored_afterwards(self, hub, monkeypatch):
        """The env var is process-wide: a mirror download must not repoint
        every later one."""
        monkeypatch.setenv("HF_ENDPOINT", "http://original.local")
        job_id = _seed_job(status="queued")
        seen: list[str] = []

        def record(**_kwargs):
            seen.append(os.environ["HF_ENDPOINT"])
            return str(hub)

        with (
            patch("huggingface_hub.snapshot_download", side_effect=record),
            patch.object(models_tool, "_publish_job"),
        ):
            models_tool._run_download(
                job_id, dict(self.SOURCE, endpoint="http://mirror.local")
            )

        assert seen == ["http://mirror.local"]
        assert os.environ["HF_ENDPOINT"] == "http://original.local"

    def test_a_source_with_no_endpoint_leaves_the_environment_clean(
        self, hub, monkeypatch
    ):
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        job_id = _seed_job(status="queued")

        with (
            patch("huggingface_hub.snapshot_download", return_value=str(hub)),
            patch.object(models_tool, "_publish_job"),
        ):
            models_tool._run_download(job_id, self.SOURCE)

        assert "HF_ENDPOINT" not in os.environ
        assert models_tool.get_download(job_id)["status"] == "completed"


class TestCancelDownload:
    def test_a_queued_job_is_cancelled_without_waiting_for_the_thread(self):
        job_id = _seed_job(status="queued")
        events: list = []

        with patch.object(
            models_tool, "_publish_job", side_effect=lambda e, j: events.append(e)
        ):
            cancelled = models_tool.cancel_download(job_id)

        assert cancelled["status"] == "cancelled"
        assert cancelled["finished_at"]
        assert events == [models_tool.EVENT_CANCELLED]

    def test_a_running_job_is_only_asked_to_stop(self):
        job_id = _seed_job(status="running")

        with patch.object(models_tool, "_publish_job") as publish:
            updated = models_tool.cancel_download(job_id)

        assert updated["status"] == "running"
        assert updated["cancel_requested"] is True
        assert job_id in models_tool._cancelled
        publish.assert_not_called()

    @pytest.mark.parametrize("state", models_tool.TERMINAL_STATES)
    def test_a_finished_job_is_returned_unchanged(self, state):
        """Cancelling a completed download must not rewrite it as cancelled."""
        job_id = _seed_job(status=state, finished_at="2024-01-01T00:00:00+00:00")

        with patch.object(models_tool, "_publish_job") as publish:
            result = models_tool.cancel_download(job_id)

        assert result["status"] == state
        assert job_id not in models_tool._cancelled
        publish.assert_not_called()


# ── The hub CLI cross-check ──────────────────────────────────────────────────


class TestHfCacheVerify:
    def test_a_cli_that_cannot_be_run_is_unavailable_not_failed(self, hub):
        """An optional extra check that errors must not condemn the copy."""
        with (
            patch.object(models_tool.shutil, "which", return_value="/usr/bin/hf"),
            patch.object(
                models_tool.subprocess, "run", side_effect=OSError("Exec format error")
            ),
        ):
            verdict = models_tool._hf_cache_verify("acme/plain-7b", "abc123")

        assert verdict["state"] == "unavailable"
        assert "Exec format error" in verdict["reason"]

    def test_a_cli_that_hangs_past_its_timeout_is_unavailable(self, hub):
        with (
            patch.object(models_tool.shutil, "which", return_value="/usr/bin/hf"),
            patch.object(
                models_tool.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("hf", 900),
            ),
        ):
            verdict = models_tool._hf_cache_verify("acme/plain-7b", None)

        assert verdict["state"] == "unavailable"

    def test_the_cli_is_pointed_at_our_cache_and_our_revision(self, hub):
        recorded: list[list[str]] = []

        def run(argv, **_kwargs):
            recorded.append(argv)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        with (
            patch.object(models_tool.shutil, "which", return_value="/usr/bin/hf"),
            patch.object(models_tool.subprocess, "run", side_effect=run),
        ):
            verdict = models_tool._hf_cache_verify("acme/plain-7b", "abc123")

        assert verdict == {"state": hub_cache.STATE_VERIFIED, "reason": "ok"}
        assert recorded[0] == [
            "hf",
            "cache",
            "verify",
            "acme/plain-7b",
            "--cache-dir",
            str(models_tool.hub_dir()),
            "--json",
            "--revision",
            "abc123",
        ]

    def test_no_revision_means_no_revision_flag(self, hub):
        recorded: list[list[str]] = []

        def run(argv, **_kwargs):
            recorded.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "hash mismatch")

        with (
            patch.object(models_tool.shutil, "which", return_value="/usr/bin/hf"),
            patch.object(models_tool.subprocess, "run", side_effect=run),
        ):
            verdict = models_tool._hf_cache_verify("acme/plain-7b", None)

        assert "--revision" not in recorded[0]
        assert verdict == {
            "state": hub_cache.STATE_PARTIAL,
            "reason": "hash mismatch",
        }


# ── Reading what a node said ─────────────────────────────────────────────────


class TestParseReport:
    def test_nothing_on_stdout_is_no_report(self):
        assert models_tool._parse_report("") is None
        assert models_tool._parse_report("   \n\n") is None

    def test_a_node_without_python_produced_no_report(self):
        assert models_tool._parse_report("bash: python3: command not found") is None

    def test_a_truncated_json_line_is_skipped_and_the_real_report_still_read(self):
        """The verifier's report is the last complete JSON object, and a shell
        that appended noise after it must not hide it."""
        stdout = '{"state": "verified", "bytes_present": 12}\n{"state": "part'

        assert models_tool._parse_report(stdout) == {
            "state": "verified",
            "bytes_present": 12,
        }

    def test_json_that_is_not_a_report_is_not_read_as_one(self):
        assert models_tool._parse_report('{"warning": "disk nearly full"}') is None
        assert models_tool._parse_report("[1, 2, 3]") is None

    def test_the_last_report_wins(self):
        stdout = '{"state": "absent"}\n{"state": "verified"}'

        assert models_tool._parse_report(stdout) == {"state": "verified"}


# ── Asking a node how many bytes have landed ─────────────────────────────────


class _StubSSH(SSHClient):
    """A node that answers exec with a canned stdout, or raises."""

    def __init__(self, stdout="", error=None, returncode=0):
        self.execs: list[tuple[str, str]] = []
        self.copies: list[tuple[str, str, str]] = []
        self.copy_dirs: list[tuple[str, str, str]] = []
        self._stdout = stdout
        self._error = error
        self._returncode = returncode

    def exec(self, host, command, timeout=30, batch_mode=True):
        self.execs.append((host, command))
        if self._error is not None:
            raise self._error
        stdout = (
            self._stdout(len(self.execs)) if callable(self._stdout) else self._stdout
        )
        return SSHResult(returncode=self._returncode, stdout=stdout, stderr="")

    def copy(self, local_path, host, remote_path, timeout=30):
        self.copies.append((local_path, host, remote_path))

    def copy_dir(self, local_dir, host, remote_dir, timeout=60):
        self.copy_dirs.append((local_dir, host, remote_dir))


class TestRemoteBytes:
    def test_the_verifier_is_asked_rather_than_du(self, hub):
        """``du -b`` is GNU-only and ``du`` alone reports blocks, not bytes."""
        ssh = _StubSSH(stdout='{"bytes": 8192}')

        assert models_tool._remote_bytes(ssh, "n1", "/staging/repo") == 8192
        command = ssh.execs[0][1]
        assert models_tool.REMOTE_HELPER_NAME in command
        assert shlex.split(command)[-3:] == ["du", "--path", "/staging/repo"]

    def test_an_unreachable_node_reports_no_progress_rather_than_failing(self):
        """Progress is decoration; losing it must not fail the transfer."""
        ssh = _StubSSH(
            error=SSHError(
                error_type=SSHErrorType.NETWORK, host="n1", message="no route"
            )
        )

        assert models_tool._remote_bytes(ssh, "n1", "/staging") == 0

    def test_output_that_is_not_a_byte_count_reads_as_zero(self):
        assert models_tool._remote_bytes(_StubSSH(stdout="du: bad flag"), "n", "/") == 0
        assert (
            models_tool._remote_bytes(_StubSSH(stdout='{"state": "absent"}'), "n", "/")
            == 0
        )

    def test_noise_before_the_answer_is_ignored(self):
        ssh = _StubSSH(stdout='Warning: unknown locale\n{"bytes": 42}')

        assert models_tool._remote_bytes(ssh, "n1", "/staging") == 42


class TestProgressPoller:
    def test_a_node_with_nothing_on_disk_yet_reports_no_progress(self):
        """Zero is "the transfer has not started", not a progress update."""
        answers = ['{"bytes": 0}', '{"bytes": 0}', '{"bytes": 4096}']
        ssh = _StubSSH(stdout=lambda n: answers[min(n, len(answers)) - 1])
        updates: list[dict] = []

        with patch.object(models_tool, "publish_event") as published:
            with models_tool._ProgressPoller(
                ssh,
                "n1",
                "/staging/repo",
                "acme/plain-7b",
                8192,
                on_progress=updates.append,
                interval=0.01,
            ) as poller:
                deadline = time.monotonic() + 5
                while not updates and time.monotonic() < deadline:
                    time.sleep(0.01)

        assert updates, "the poller never reported the bytes that landed"
        assert all(
            update
            == {
                "model": "acme/plain-7b",
                "node": "n1",
                "bytes_done": 4096,
                "bytes_total": 8192,
            }
            for update in updates
        )
        # The two answers of zero were polls like any other, and neither of
        # them produced an update.
        assert len(ssh.execs) - len(updates) >= 2
        assert poller.bytes_done == 4096
        assert published.call_args[0][0] is models_tool.EVENT_REPLICATION_PROGRESS


# ── Publishing a verified replica ────────────────────────────────────────────


class _VerifyThenFailPublishSSH(SSHClient):
    """A node that verifies the staged copy but cannot rename it into place.

    The verify answers are keyed on ``--repo`` so the pre-flight check of the
    final directory says "absent" (nothing there yet) while the check of the
    staging directory says "verified".
    """

    def __init__(self, staging_dir: str, publish_stderr: str):
        self.staging_dir = staging_dir
        self.publish_stderr = publish_stderr
        self.execs: list[str] = []
        self.copies: list[tuple[str, str, str]] = []
        self.copy_dirs: list[tuple[str, str, str]] = []

    def exec(self, host, command, timeout=30, batch_mode=True):
        self.execs.append(command)
        parts = shlex.split(command)
        if "verify" in parts:
            repo = parts[parts.index("--repo") + 1]
            if repo == self.staging_dir:
                return SSHResult(0, json.dumps(self._verified()), "")
            return SSHResult(0, json.dumps({"state": hub_cache.STATE_ABSENT}), "")
        if "du" in parts:
            return SSHResult(0, '{"bytes": 12345}', "")
        if command.startswith("mkdir"):
            return SSHResult(0, "", "")
        return SSHResult(1, "", self.publish_stderr)

    def _verified(self) -> dict:
        return {
            "state": hub_cache.STATE_VERIFIED,
            "reason": "",
            "revision": SAMPLE_COMMIT,
            "bytes_present": sum(len(v) for v in SAMPLE_FILES.values()),
            "files_present": len(SAMPLE_FILES),
            "missing": [],
            "missing_count": 0,
            "verified_at": "2024-05-01T00:00:00+00:00",
        }

    def copy(self, local_path, host, remote_path, timeout=30):
        self.copies.append((local_path, host, remote_path))

    def copy_dir(self, local_dir, host, remote_dir, timeout=60):
        self.copy_dirs.append((local_dir, host, remote_dir))


class TestPublishFailure:
    def test_a_replica_that_verifies_but_cannot_be_renamed_is_not_published(self, hub):
        """Verified is not published: the rename is what publishes it, and a
        read-only hub on the node has to be reported, not assumed away."""
        sample_entry(hub)
        staging = f"{hub}/{models_tool.STAGING_DIRNAME}/{models_tool.repo_dir_name(SAMPLE_MODEL)}"
        ssh = _VerifyThenFailPublishSSH(staging, "mv: Read-only file system")

        result = models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=ssh)

        assert result["ok"] is False
        entry = result["results"][0]
        assert entry["state"] == hub_cache.STATE_VERIFIED
        assert entry["published"] is False
        assert entry["ok"] is False
        assert entry["error"] == "mv: Read-only file system"
        assert entry["reason"] == "verified, but the rename into place failed"
        assert entry["bytes_done"] == 12345
        assert entry["bytes_verified"] == sum(len(v) for v in SAMPLE_FILES.values())
        assert entry["token_sent"] is False

    def test_the_failing_command_really_was_the_rename(self, hub):
        sample_entry(hub)
        staging = f"{hub}/{models_tool.STAGING_DIRNAME}/{models_tool.repo_dir_name(SAMPLE_MODEL)}"
        ssh = _VerifyThenFailPublishSSH(staging, "mv: Read-only file system")

        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=ssh)

        assert ssh.execs[-1] == models_tool._publish_command(
            staging, f"{hub}/{models_tool.repo_dir_name(SAMPLE_MODEL)}"
        )


# ── Which models a deployment is holding open ────────────────────────────────


class _StubRecords:
    def __init__(self, records):
        self._records = records

    def load(self):
        if isinstance(self._records, Exception):
            raise self._records
        return self._records


class TestModelsInUse:
    def _install(self, monkeypatch, deployments, recipes=()):
        monkeypatch.setattr(tools_pkg, "deployment_records", _StubRecords(deployments))
        monkeypatch.setattr(tools_pkg, "recipes", _StubRecipes(list(recipes)))

    def test_a_deployment_whose_model_is_unknown_holds_nothing_open(self, monkeypatch):
        """A recipe with no model resolves to the literal "unknown"; deleting
        a model must not be blocked by it."""
        self._install(
            monkeypatch,
            [
                {"id": "d1", "status": "running", "recipe_id": "r1", "params": {}},
                {"id": "d2", "status": "running", "recipe_id": "r2", "params": {}},
                {"id": "d3", "status": "running", "recipe_id": "r3", "params": {}},
            ],
            recipes=[
                {"id": "r1", "model": "unknown"},
                {"id": "r2", "model": ""},
                {"id": "r3", "model": "acme/real"},
            ],
        )

        assert models_tool.models_in_use() == {"acme/real": ["d3"]}

    def test_deployment_records_that_cannot_be_read_block_no_deletion(
        self, monkeypatch
    ):
        self._install(monkeypatch, OSError("deployments.json is corrupt"))

        assert models_tool.models_in_use() == {}

    def test_a_model_in_use_is_what_makes_delete_refuse(self, hub, monkeypatch):
        (hub / models_tool.repo_dir_name("acme/plain-7b")).mkdir()
        self._install(
            monkeypatch,
            [{"id": "d1", "status": "running", "params": {"model": "Acme/Plain-7B"}}],
        )

        with pytest.raises(ValueError, match="in use by running deployment"):
            models_tool.delete_model("acme/plain-7b")
