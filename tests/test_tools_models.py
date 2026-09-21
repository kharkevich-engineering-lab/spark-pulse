"""Tests for the model catalogue, download jobs and guards."""

import importlib
import json
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# NB: pytest-env forces SIMULATION_MODE=1, so `from spark_pulse.tools import
# models` hands back the mock (the mock imports the real module during package
# init, so the submodule is already in sys.modules and never rebound). Resolve
# the real module explicitly — that is what this file tests.
models_tool = importlib.import_module("spark_pulse.tools.models")

from spark_pulse.tools.ssh import (  # noqa: E402
    OpenSSHClient,
    SSHClient,
    SSHError,
    SSHErrorType,
    SSHResult,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────


class _RecordingSSHClient(SSHClient):
    """SSH double that records calls instead of touching the network."""

    def __init__(
        self,
        exec_returncode=0,
        exec_stderr="",
        exec_error: Exception | None = None,
        copy_error: Exception | None = None,
        stdout: str = "",
    ):
        self.execs: list[tuple[str, str]] = []
        self.copies: list[tuple[str, str, str]] = []
        self.copy_dirs: list[tuple[str, str, str]] = []
        self._exec_returncode = exec_returncode
        self._exec_stderr = exec_stderr
        self._exec_error = exec_error
        self._copy_error = copy_error
        self._stdout = stdout

    def exec(self, host, command, timeout=30, batch_mode=True):
        self.execs.append((host, command))
        if self._exec_error is not None:
            raise self._exec_error
        code = self._exec_returncode
        if callable(code):
            code = code(host)
        return SSHResult(
            returncode=code,
            stdout=self._stdout,
            stderr="" if code == 0 else self._exec_stderr,
        )

    def copy(self, local_path, host, remote_path, timeout=30):
        self.copies.append((local_path, host, remote_path))

    def copy_dir(self, local_dir, host, remote_dir, timeout=60):
        self.copy_dirs.append((local_dir, host, remote_dir))
        if self._copy_error is not None:
            raise self._copy_error


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_repo(
    hub: Path,
    model_id: str,
    revision: str,
    *,
    config_data: dict | None = None,
    weight_bytes: int = 2048,
    ref: str = "main",
) -> Path:
    repo = hub / models_tool.repo_dir_name(model_id)
    snapshot = repo / "snapshots" / revision
    snapshot.mkdir(parents=True, exist_ok=True)
    if config_data is not None:
        _write(snapshot / "config.json", json.dumps(config_data))
    (snapshot / "model.safetensors").write_bytes(b"x" * weight_bytes)
    _write(repo / "refs" / ref, revision)
    return snapshot


@pytest.fixture
def hf_home(tmp_path, monkeypatch):
    """Point HF_HOME at a temp dir with a small canned hub cache."""
    home = tmp_path / "hf"
    hub = home / "hub"
    hub.mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(home))

    _make_repo(
        hub,
        "acme/plain-7b",
        "aaaa1111",
        config_data={
            "architectures": ["LlamaForCausalLM"],
            "model_type": "llama",
            "torch_dtype": "bfloat16",
        },
        weight_bytes=4096,
    )
    _make_repo(
        hub,
        "acme/quant-70b",
        "bbbb2222",
        config_data={
            "architectures": ["Qwen3MoeForCausalLM"],
            "model_type": "qwen3_moe",
            "torch_dtype": "float16",
            "quantization_config": {
                "quant_method": "awq",
                "bits": 4,
                "group_size": 128,
            },
        },
        weight_bytes=8192,
    )
    # A repo without config.json at all.
    _make_repo(hub, "acme/no-config", "cccc3333", config_data=None)
    # Noise that must be ignored.
    (hub / "datasets--acme--corpus").mkdir()
    (hub / "version.txt").write_text("1")
    return home


@pytest.fixture(autouse=True)
def _no_recipes():
    """Catalogue tests should not depend on the developer's own recipes."""
    with patch.object(models_tool, "_recipe_index", return_value={}):
        yield


@pytest.fixture(autouse=True)
def _clean_jobs():
    yield
    models_tool._jobs.clear()
    models_tool._cancelled.clear()


# ── Catalogue ────────────────────────────────────────────────────────────────


class TestCatalogue:
    def test_hf_home_env_override(self, hf_home):
        assert models_tool.hf_home() == hf_home
        assert models_tool.hub_dir() == hf_home / "hub"

    def test_repo_dir_name_roundtrip(self):
        assert models_tool.repo_dir_name("org/name") == "models--org--name"
        assert models_tool._model_id_from_dir("models--org--name") == "org/name"

    def test_list_models_finds_cached_repos(self, hf_home):
        ids = [m["id"] for m in models_tool.list_models()]
        assert ids == ["acme/no-config", "acme/plain-7b", "acme/quant-70b"]

    def test_entry_shape(self, hf_home):
        entry = models_tool.get_model("acme/plain-7b")
        assert entry is not None
        assert entry["revision"] == "aaaa1111"
        assert entry["source_type"] == "hf_cache"
        assert entry["path"].endswith("snapshots/aaaa1111")
        assert entry["size_bytes"] >= 4096
        assert entry["last_modified"]
        assert entry["config"]["architectures"] == ["LlamaForCausalLM"]
        assert entry["config"]["model_type"] == "llama"
        assert entry["config"]["torch_dtype"] == "bfloat16"
        assert entry["config"]["quantization"] == []
        assert entry["revisions"][0]["refs"] == ["main"]

    def test_quantization_config_keys_summarised(self, hf_home):
        entry = models_tool.get_model("acme/quant-70b")
        assert entry["config"]["quantization"] == ["bits", "group_size", "quant_method"]
        assert entry["config"]["quantization_method"] == "awq"

    def test_missing_config_json_is_none(self, hf_home):
        entry = models_tool.get_model("acme/no-config")
        assert entry["config"] is None

    def test_get_model_unknown_returns_none(self, hf_home):
        assert models_tool.get_model("acme/does-not-exist") is None

    def test_empty_hub_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_HOME", str(tmp_path / "nothing"))
        assert models_tool.list_models() == []

    def test_referenced_by_recipes(self, hf_home):
        with patch.object(
            models_tool,
            "_recipe_index",
            return_value={"acme/plain-7b": ["recipes/plain.yaml"]},
        ):
            entry = models_tool.get_model("acme/plain-7b")
            assert entry["referenced_by"] == ["recipes/plain.yaml"]
            other = models_tool.get_model("acme/quant-70b")
            assert other["referenced_by"] == []

    def test_local_path_source_lists_config_dirs(self, hf_home, tmp_path):
        root = tmp_path / "models"
        (root / "team" / "sft-8b").mkdir(parents=True)
        _write(
            root / "team" / "sft-8b" / "config.json",
            json.dumps({"model_type": "llama", "architectures": ["LlamaForCausalLM"]}),
        )
        (root / "team" / "not-a-model").mkdir(parents=True)
        with patch.object(
            models_tool,
            "list_sources",
            return_value=[{"name": "local", "type": "local_path", "path": str(root)}],
        ):
            local = [m for m in models_tool.list_models() if m["source"] == "local"]
        assert len(local) == 1
        assert local[0]["id"] == "team/sft-8b"
        assert local[0]["source_type"] == "local_path"
        assert local[0]["config"]["model_type"] == "llama"


# ── Sources ──────────────────────────────────────────────────────────────────


class TestSources:
    def test_default_source_when_unconfigured(self):
        with patch.object(type(models_tool.config), "model_sources", []):
            sources = models_tool.list_sources()
        assert sources[0]["name"] == "hf"
        assert sources[0]["endpoint"] == "https://huggingface.co"

    def test_get_source_by_name_and_default(self):
        configured = [
            {"name": "hf", "type": "hf_hub", "endpoint": "https://huggingface.co"},
            {"name": "mirror", "type": "hf_hub", "endpoint": "http://mirror.local"},
        ]
        with patch.object(models_tool, "list_sources", return_value=configured):
            assert models_tool.get_source(None)["name"] == "hf"
            assert models_tool.get_source("mirror")["endpoint"] == "http://mirror.local"
            with pytest.raises(ValueError, match="Unknown model source"):
                models_tool.get_source("nope")

    def test_save_sources_validates(self):
        with patch.object(models_tool.config, "update") as update:
            saved = models_tool.save_sources(
                [
                    {"name": "hf", "type": "hf_hub"},
                    {"name": "local", "type": "local_path", "path": "/models"},
                ]
            )
        assert saved[0]["endpoint"] == "https://huggingface.co"
        assert saved[1]["path"] == "/models"
        update.assert_called_once()

    @pytest.mark.parametrize(
        "bad,message",
        [
            ([{"type": "hf_hub"}], "name is required"),
            ([{"name": "a"}, {"name": "a"}], "Duplicate source name"),
            ([{"name": "a", "type": "ftp"}], "Unknown source type"),
            ([{"name": "a", "type": "local_path"}], "needs a path"),
            (["not-an-object"], "must be an object"),
        ],
    )
    def test_save_sources_rejects_bad_input(self, bad, message):
        with patch.object(models_tool.config, "update"):
            with pytest.raises(ValueError, match=message):
                models_tool.save_sources(bad)


# ── Download jobs ────────────────────────────────────────────────────────────


def _wait_for(job_id, states, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = models_tool.get_download(job_id)
        if job and job["status"] in states:
            return job
        time.sleep(0.02)
    raise AssertionError(
        f"Job {job_id} never reached {states}: {models_tool.get_download(job_id)}"
    )


class TestDownloadJobs:
    def test_state_machine_queued_to_completed(self, hf_home):
        events = []
        snapshot_path = hf_home / "hub" / "models--acme--plain-7b" / "snapshots" / "x"
        snapshot_path.mkdir(parents=True)
        (snapshot_path / "model.safetensors").write_bytes(b"y" * 1000)

        with (
            patch.object(
                models_tool,
                "_plan_files",
                return_value=[{"path": "model.safetensors", "size": 1000}],
            ),
            patch.object(
                models_tool,
                "publish_event",
                side_effect=lambda event, resource, metadata: events.append(event),
            ),
            patch(
                "huggingface_hub.hf_hub_download",
                return_value=str(snapshot_path / "model.safetensors"),
            ),
        ):
            job = models_tool.start_download("acme/plain-7b")
            assert job["status"] == "queued"
            assert job["bytes_total"] == 1000
            done = _wait_for(job["id"], ("completed", "failed"))

        assert done["status"] == "completed"
        # The snapshot directory, worked back from the file inside it.
        assert done["path"] == str(snapshot_path)
        assert done["bytes_done"] == 1000
        assert done["finished_at"]
        assert models_tool.EVENT_QUEUED in events
        assert models_tool.EVENT_STARTED in events
        assert models_tool.EVENT_COMPLETED in events

    def test_failure_records_error(self, hf_home):
        with (
            patch.object(models_tool, "_plan_files", return_value=[]),
            patch(
                "huggingface_hub.snapshot_download", side_effect=RuntimeError("boom")
            ),
        ):
            job = models_tool.start_download("acme/plain-7b")
            done = _wait_for(job["id"], ("failed", "completed"))
        assert done["status"] == "failed"
        assert done["error"] == "boom"

    def test_a_permission_denied_carries_the_remedy(self, hf_home):
        """``[Errno 13] … /.locks/models--…`` is not a downloader bug.

        It is a hub cache an engine container wrote as root, so Hugging Face
        cannot take its own lock. The errno on its own sends the operator
        looking in the wrong place; the job says which directory and which
        command instead.
        """
        lock = models_tool.hub_dir() / ".locks" / "models--acme--plain-7b"
        with (
            patch.object(models_tool, "_plan_files", return_value=[]),
            patch(
                "huggingface_hub.snapshot_download",
                side_effect=PermissionError(13, "Permission denied", str(lock)),
            ),
        ):
            job = models_tool.start_download("acme/plain-7b")
            done = _wait_for(job["id"], ("failed", "completed"))
        assert done["status"] == "failed"
        assert str(models_tool.hub_dir()) in done["error"]
        assert "sudo chown -R $USER" in done["error"]

    def test_snapshot_download_receives_source_settings(self, hf_home):
        source = {
            "name": "mirror",
            "type": "hf_hub",
            "endpoint": "http://mirror.local",
            "token_secret": "",
        }
        with (
            patch.object(models_tool, "_plan_files", return_value=[]),
            patch.object(models_tool, "list_sources", return_value=[source]),
            patch(
                "huggingface_hub.snapshot_download", return_value=str(hf_home)
            ) as download,
        ):
            job = models_tool.start_download(
                "acme/plain-7b",
                source="mirror",
                revision="v1",
                allow_patterns=["*.safetensors"],
            )
            _wait_for(job["id"], ("completed", "failed"))
        kwargs = download.call_args.kwargs
        assert kwargs["repo_id"] == "acme/plain-7b"
        assert kwargs["revision"] == "v1"
        assert kwargs["allow_patterns"] == ["*.safetensors"]
        assert kwargs["cache_dir"] == str(models_tool.hub_dir())
        # Passed straight to the call, not through the process-global
        # HF_ENDPOINT env var — see TestRunDownload in
        # test_tools_models_coverage.py for why.
        assert kwargs["endpoint"] == "http://mirror.local"

    def test_local_path_source_cannot_download(self, hf_home):
        with patch.object(
            models_tool,
            "list_sources",
            return_value=[{"name": "local", "type": "local_path", "path": "/models"}],
        ):
            with pytest.raises(ValueError, match="nothing to download"):
                models_tool.start_download("acme/plain-7b", source="local")

    def test_empty_model_rejected(self):
        with pytest.raises(ValueError, match="model is required"):
            models_tool.start_download("  ")

    def test_cancel_queued_job(self, hf_home, monkeypatch):
        """Cancelling before the download thread has even run must not let
        it start — deterministically, not by winning a race against it.

        ``start_download`` fires a real background thread, and that thread
        can reach ``_run_download``'s "am I cancelled" check before this test
        gets to call ``cancel_download`` at all: on a fast enough scheduler
        the job would already be "running" by the time cancellation is
        requested, which is a different scenario (see
        ``test_a_download_finishing_after_a_cancel_request_is_not_reported_as_cancelled``
        below) with a different correct outcome. Deferring the thread's
        actual work until this test says so removes the race rather than
        hoping to win it.
        """

        class DeferredThread:
            """Stands in for ``threading.Thread``: records the call instead
            of running it, so nothing happens until ``run_now`` is called."""

            instances: list["DeferredThread"] = []

            def __init__(self, *, target, args=(), name=None, daemon=None):
                self._target = target
                self._args = args
                DeferredThread.instances.append(self)

            def start(self) -> None:
                pass

            def run_now(self) -> None:
                self._target(*self._args)

        DeferredThread.instances = []
        monkeypatch.setattr(models_tool.threading, "Thread", DeferredThread)

        with (
            patch.object(models_tool, "_plan_files", return_value=[]),
            patch("huggingface_hub.snapshot_download") as download,
        ):
            job = models_tool.start_download("acme/plain-7b")
            cancelled = models_tool.cancel_download(job["id"])
            assert cancelled["status"] == "cancelled"
            # Running the deferred thread now must still be a no-op: the
            # cancellation happened before any of its code ran.
            DeferredThread.instances[0].run_now()

        download.assert_not_called()
        assert models_tool.get_download(job["id"])["status"] == "cancelled"

    def test_a_download_finishing_after_a_cancel_request_is_not_reported_as_cancelled(
        self, hf_home
    ):
        """``snapshot_download`` cannot be interrupted once it is running: a
        cancel requested while it is in flight cannot stop it, and the model
        it fetches is real and on disk. The job must say so — "cancelled"
        here would be a real, usable download reported as if it never
        happened, and would fail a scheduled deploy waiting on a model that
        actually arrived."""
        release = threading.Event()

        def _slow(**kwargs):
            release.wait(3)
            return str(models_tool.hub_dir())

        with (
            patch.object(models_tool, "_plan_files", return_value=[]),
            patch("huggingface_hub.snapshot_download", side_effect=_slow),
        ):
            job = models_tool.start_download("acme/plain-7b")
            _wait_for(job["id"], ("running",))
            models_tool.cancel_download(job["id"])
            release.set()
            done = _wait_for(job["id"], ("cancelled", "completed", "failed"))
        assert done["status"] == "completed"

    def test_a_cancel_stops_the_job_before_the_next_file(self, hf_home):
        """The bug this closes: cancelling a running download did nothing.

        ``snapshot_download`` is one blocking call, so the flag was only ever
        read before it started and after it returned — twenty cancel requests
        over two minutes of a 68 GB download, and only a restart stopped it.
        With a file plan the loop reads the flag between files, so the cancel
        lands on the next boundary and the job says ``cancelled``.
        """
        hub = hf_home / "hub"
        snapshot = hub / "models--acme--plain-7b" / "snapshots" / "x"
        snapshot.mkdir(parents=True, exist_ok=True)
        plan = [{"path": f"shard-{i}.safetensors", "size": 100} for i in range(1, 5)]
        fetched: list[str] = []
        cancel_after_first = threading.Event()

        def _fetch(**kwargs):
            name = kwargs["filename"]
            fetched.append(name)
            path = snapshot / name
            path.write_bytes(b"z" * 100)
            cancel_after_first.set()
            # Give the cancel a moment to arrive, so the stop is the flag
            # being read rather than the loop simply running out of files.
            time.sleep(0.05)
            return str(path)

        with (
            patch.object(models_tool, "_plan_files", return_value=plan),
            patch("huggingface_hub.hf_hub_download", side_effect=_fetch),
        ):
            job = models_tool.start_download("acme/plain-7b")
            assert cancel_after_first.wait(3)
            models_tool.cancel_download(job["id"])
            done = _wait_for(job["id"], ("cancelled", "completed", "failed"))

        assert done["status"] == "cancelled"
        assert done["error"] is None
        assert len(fetched) < len(plan), "the loop ran to the end anyway"
        # Nothing is swept up: the files already fetched stay, and so would the
        # `.incomplete` blob of the one that was interrupted — which is what
        # the next job resumes from.
        assert (snapshot / fetched[0]).exists()

    def test_a_second_request_for_different_files_is_refused_by_id(self, hf_home):
        """Not silently handed the running job — that is the other half of #134.

        An operator who cancels an unfiltered download and asks for one
        quantisation must not be handed back the job they were escaping.
        """
        release = threading.Event()

        def _slow(**kwargs):
            release.wait(3)
            return str(models_tool.hub_dir())

        plan = [{"path": "big.gguf", "size": 10}]
        with (
            patch.object(models_tool, "_plan_files", return_value=plan),
            patch("huggingface_hub.hf_hub_download", side_effect=_slow),
        ):
            first = models_tool.start_download("acme/plain-7b")
            _wait_for(first["id"], ("running",))

            # The same request is the same download, and comes back as itself.
            assert models_tool.start_download("acme/plain-7b")["id"] == first["id"]

            with pytest.raises(models_tool.DownloadInProgress) as raised:
                models_tool.start_download(
                    "acme/plain-7b", allow_patterns=["*PQ2_0.gguf"]
                )
            release.set()
            _wait_for(first["id"], ("completed", "failed", "cancelled"))

        assert raised.value.job["id"] == first["id"]
        assert first["id"] in str(raised.value)

    def test_bytes_done_never_passes_bytes_total_on_a_resumed_job(self, hf_home):
        """A resumed partial counts up to its file's size in the set, no more.

        The 10.3 GB of 7.2 GB an operator saw came from counting the whole
        cache entry: a cancelled unfiltered job's blobs were still sitting in
        it, and the filtered job that followed reported them as its own.
        """
        hub = hf_home / "hub"
        repo = hub / "models--acme--plain-7b"
        snapshot = repo / "snapshots" / "x"
        snapshot.mkdir(parents=True, exist_ok=True)
        # What an earlier, wider download left behind in the same entry.
        blobs = repo / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        (blobs / "leftover-from-the-cancelled-job").write_bytes(b"x" * 50_000)

        plan = [{"path": "only.gguf", "size": 1000}]

        def _fetch(**kwargs):
            # The resumed file arrives bigger than the plan says it weighs,
            # which is the clamp's whole reason for being.
            path = snapshot / kwargs["filename"]
            path.write_bytes(b"y" * 4000)
            return str(path)

        with (
            patch.object(models_tool, "_plan_files", return_value=plan),
            patch("huggingface_hub.hf_hub_download", side_effect=_fetch),
        ):
            job = models_tool.start_download("acme/plain-7b")
            done = _wait_for(job["id"], ("completed", "failed"))

        assert done["status"] == "completed"
        assert done["bytes_total"] == 1000
        assert done["bytes_done"] == 1000

    def test_a_filtered_download_records_its_filter_on_the_marker(self, hf_home):
        """#135: nothing on disk said the download was deliberately narrow."""
        hub = hf_home / "hub"
        repo = hub / "models--acme--plain-7b"
        snapshot = repo / "snapshots" / "aaaa1111"
        snapshot.mkdir(parents=True, exist_ok=True)

        def _fetch(**kwargs):
            path = snapshot / kwargs["filename"]
            path.write_bytes(b"g" * 10)
            return str(path)

        with (
            patch.object(
                models_tool,
                "_plan_files",
                return_value=[{"path": "model.PQ2_0.gguf", "size": 10}],
            ),
            patch("huggingface_hub.hf_hub_download", side_effect=_fetch),
        ):
            job = models_tool.start_download(
                "acme/plain-7b", allow_patterns=["*PQ2_0.gguf"]
            )
            _wait_for(job["id"], ("completed", "failed"))

        marker = models_tool.hub_cache.read_marker(str(repo))
        assert marker["allow_patterns"] == ["*PQ2_0.gguf"]
        assert marker["revision"] == "aaaa1111"

    def test_an_unfiltered_download_records_no_marker(self, hf_home):
        """The manifest already says what a whole-revision download owes."""
        hub = hf_home / "hub"
        repo = hub / "models--acme--plain-7b"
        snapshot = repo / "snapshots" / "aaaa1111"

        with (
            patch.object(
                models_tool,
                "_plan_files",
                return_value=[{"path": "model.safetensors", "size": 4096}],
            ),
            patch(
                "huggingface_hub.hf_hub_download",
                return_value=str(snapshot / "model.safetensors"),
            ),
        ):
            job = models_tool.start_download("acme/plain-7b")
            _wait_for(job["id"], ("completed", "failed"))

        assert models_tool.hub_cache.read_marker(str(repo)) is None

    def test_cancel_unknown_job(self):
        assert models_tool.cancel_download("nope") is None

    def test_list_and_clear_downloads(self, hf_home):
        with (
            patch.object(models_tool, "_plan_files", return_value=[]),
            patch("huggingface_hub.snapshot_download", return_value=str(hf_home)),
        ):
            job = models_tool.start_download("acme/plain-7b")
            _wait_for(job["id"], ("completed", "failed"))
        assert [j["id"] for j in models_tool.list_downloads()] == [job["id"]]
        assert models_tool.clear_finished_downloads() == 1
        assert models_tool.list_downloads() == []


class TestDiskSpaceGuard:
    def test_raises_when_not_enough_space(self, hf_home):
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")
        with patch("shutil.disk_usage", return_value=usage(100, 90, 10)):
            with pytest.raises(ValueError, match="Not enough free disk space"):
                models_tool.check_disk_space(1_000_000)

    def test_passes_when_enough_space(self, hf_home):
        from collections import namedtuple

        usage = namedtuple("usage", "total used free")
        with patch("shutil.disk_usage", return_value=usage(100, 0, 100)):
            models_tool.check_disk_space(10)

    def test_unknown_estimate_skips_check(self):
        with patch("shutil.disk_usage", side_effect=AssertionError("must not run")):
            models_tool.check_disk_space(0)

    def test_start_download_refuses_without_space(self, hf_home):
        with (
            patch.object(
                models_tool,
                "_plan_files",
                return_value=[{"path": "model.safetensors", "size": 10**15}],
            ),
            patch.object(
                models_tool, "check_disk_space", side_effect=ValueError("no space")
            ),
        ):
            with pytest.raises(ValueError, match="no space"):
                models_tool.start_download("acme/plain-7b")
        assert models_tool.list_downloads() == []


# ── Distribution ─────────────────────────────────────────────────────────────


class TestDistribution:
    def test_sync_requires_cached_model(self, hf_home):
        with pytest.raises(ValueError, match="not in local cache"):
            models_tool.replicate_to_nodes("acme/missing", ["node1"])

    def test_sync_requires_nodes(self, hf_home):
        with pytest.raises(ValueError, match="No nodes specified"):
            models_tool.replicate_to_nodes("acme/plain-7b", [])

    def test_sync_reports_per_node_failure(self, hf_home):
        client = _RecordingSSHClient(copy_error=RuntimeError("rsync failed: no route"))
        result = models_tool.replicate_to_nodes("acme/plain-7b", ["n1"], client=client)
        assert result["ok"] is False
        assert result["results"][0]["error"] == "rsync failed: no route"
        assert result["results"][0]["published"] is False

    def test_sync_reports_mkdir_failure(self, hf_home):
        client = _RecordingSSHClient(exec_returncode=1, exec_stderr="permission denied")
        result = models_tool.replicate_to_nodes("acme/plain-7b", ["n1"], client=client)
        assert result["ok"] is False
        assert result["results"][0]["error"] == "permission denied"
        assert client.copies == []

    def test_sync_reports_ssh_error(self, hf_home):
        client = _RecordingSSHClient(
            exec_error=SSHError(
                error_type=SSHErrorType.TIMEOUT, host="n1", message="timed out"
            )
        )
        result = models_tool.replicate_to_nodes("acme/plain-7b", ["n1"], client=client)
        assert result["ok"] is False
        assert "timed out" in result["results"][0]["error"]

    def test_sync_refuses_a_local_copy_that_does_not_verify(self, hf_home, tmp_path):
        """Replicating a broken source only spreads it."""
        snapshot = (
            models_tool.hub_dir() / "models--acme--plain-7b" / "snapshots" / "aaaa1111"
        )
        for entry in snapshot.iterdir():
            entry.unlink()
        client = _RecordingSSHClient()
        with pytest.raises(ValueError, match="is partial"):
            models_tool.replicate_to_nodes("acme/plain-7b", ["n1"], client=client)
        assert client.copy_dirs == []

    def test_a_node_that_cannot_answer_is_never_published_to(self, hf_home):
        """No report means no proof, and no proof means no publish."""
        client = _RecordingSSHClient(stdout="python3: not found")
        result = models_tool.replicate_to_nodes("acme/plain-7b", ["n1"], client=client)
        assert result["ok"] is False
        assert result["results"][0]["published"] is False
        assert "did not return a verification report" in result["results"][0]["error"]

    def test_default_client_is_openssh(self):
        assert isinstance(models_tool._make_ssh_client("ubuntu"), OpenSSHClient)


# ── Deletion ─────────────────────────────────────────────────────────────────


class _RemovingService:
    """A node that really deletes, and remembers what it was asked to.

    Deletion goes through the node's own agent now — including for the machine
    this process runs on — so the double is what stands in for the agent. The
    removal itself is the agent's (`agent/src/executor/snapshots.rs`, with its
    own tests); what belongs here is which repository and revision the control
    plane names, and what it does with the answers.
    """

    def __init__(self):
        self.asked: list[tuple[str, str]] = []

    def remove_snapshot(self, repo_path: str, revision: str = ""):
        from spark_pulse.agent import agent_pb2 as pb

        self.asked.append((repo_path, revision))
        target = (
            Path(repo_path)
            if not revision
            else Path(repo_path) / "snapshots" / revision
        )
        if not target.exists():
            return pb.SnapshotRemoval(removed=False)
        freed = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        shutil.rmtree(target)
        return pb.SnapshotRemoval(removed=True, freed_bytes=freed, paths=[str(target)])


class _EmptyService:
    """A node that holds no copy of the model."""

    def remove_snapshot(self, repo_path: str, revision: str = ""):
        from spark_pulse.agent import agent_pb2 as pb

        return pb.SnapshotRemoval(removed=False)


class TestDelete:
    def test_delete_removes_repo_dir(self, hf_home):
        service = _RemovingService()
        with patch.object(models_tool, "models_in_use", return_value={}):
            result = models_tool.delete_model(
                "acme/plain-7b", services=lambda _node: service
            )
        assert result["deleted"] == "acme/plain-7b"
        assert result["freed_bytes"] > 0
        assert not Path(result["path"]).exists()
        assert models_tool.get_model("acme/plain-7b") is None
        # The whole repository, not one revision: an operator clearing a model
        # means the blobs too, and naming a revision would leave them behind.
        assert service.asked == [(result["path"], "")]

    def test_every_named_node_is_asked_and_each_answers_for_itself(self, hf_home):
        """A 26 GB model on four Sparks used to be deleted from one of them,
        and the page then said it was gone."""
        asked: list[str] = []

        def _resolve(node):
            # One filesystem in this test, so only the control node holds the
            # copy; the peers answer for a repository they do not have.
            asked.append(node.label)
            return _RemovingService() if node.is_self else _EmptyService()

        with patch.object(models_tool, "models_in_use", return_value={}):
            result = models_tool.delete_model(
                "acme/plain-7b", nodes=["10.0.0.11", "10.0.0.12"], services=_resolve
            )

        assert sorted(asked) == ["10.0.0.11", "10.0.0.12", "control"]
        assert [row["node"] for row in result["nodes"]] == [
            "",
            "10.0.0.11",
            "10.0.0.12",
        ]
        # Only this machine had a copy, and the answer says which did.
        assert [row["removed"] for row in result["nodes"]] == [True, False, False]

    def test_a_node_that_cannot_be_asked_is_reported_not_assumed_clean(self, hf_home):
        def _resolve(node):
            if node.is_self:
                return _RemovingService()
            raise RuntimeError("10.0.0.11 has no enrolled agent")

        with patch.object(models_tool, "models_in_use", return_value={}):
            result = models_tool.delete_model(
                "acme/plain-7b", nodes=["10.0.0.11"], services=_resolve
            )

        peer = result["nodes"][1]
        assert peer["removed"] is False
        assert "no enrolled agent" in peer["error"]

    def test_this_machine_is_not_asked_twice_when_it_is_also_named(self, hf_home):
        """The control node is always a target; naming its address as well
        must not delete it, fail, and then report both."""
        asked: list[str] = []

        def _resolve(node):
            asked.append(node.label)
            return _RemovingService()

        with patch.object(models_tool, "models_in_use", return_value={}):
            models_tool.delete_model(
                "acme/plain-7b", nodes=["localhost"], services=_resolve
            )

        assert asked == ["control"]

    def test_delete_unknown_model(self, hf_home):
        with patch.object(models_tool, "models_in_use", return_value={}):
            with pytest.raises(ValueError, match="not in local cache"):
                models_tool.delete_model("acme/missing")

    def test_delete_refuses_when_deployment_running(self, hf_home):
        with patch.object(
            models_tool, "models_in_use", return_value={"acme/plain-7b": ["dep123"]}
        ):
            with pytest.raises(ValueError, match="in use by running deployment"):
                models_tool.delete_model("acme/plain-7b")
        assert models_tool.get_model("acme/plain-7b") is not None

    def test_models_in_use_from_running_deployments(self):
        deployments = [
            {"id": "d1", "status": "running", "recipe_id": "r1", "params": {}},
            {"id": "d2", "status": "stopped", "recipe_id": "r2", "params": {}},
            {
                "id": "d3",
                "status": "pending",
                "recipe_id": "r9",
                "params": {"model": "org/override"},
            },
        ]
        recipes = [
            {"id": "r1", "model": "org/from-recipe"},
            {"id": "r2", "model": "org/stopped"},
        ]

        class _Tools:
            class deployment_records:  # noqa: N801
                @staticmethod
                def load():
                    return deployments

            class recipes:  # noqa: N801
                @staticmethod
                def list_recipes():
                    return recipes

        with patch.dict("sys.modules"):
            import spark_pulse

            with patch.object(spark_pulse, "tools", _Tools, create=True):
                in_use = models_tool.models_in_use()

        assert in_use == {"org/from-recipe": ["d1"], "org/override": ["d3"]}
