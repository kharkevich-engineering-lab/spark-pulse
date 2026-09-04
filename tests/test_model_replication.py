"""Replicating a model to nodes: transfer, verify, publish.

The transport double here does not fake the work — it runs it. ``exec`` really
runs the shell command, ``copy_dir`` really runs rsync with the same flags the
real client uses, and every absolute path is rewritten into a per-node
directory first, so each "node" is a genuine filesystem with its own hub. That
is what makes it possible to assert on symlinks surviving, a truncated blob
being caught, and a resumed transfer not starting over.

What it cannot prove is anything that needs a second machine: real SSH
authentication, the fabric's throughput, or a node whose python differs from
this one's. Those wait for phase E.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from hub_cache_fixtures import (
    SAMPLE_COMMIT,
    SAMPLE_FILES,
    SAMPLE_MODEL,
    blob_for,
    sample_entry,
    truncate,
)

models_tool = importlib.import_module("spark_pulse.tools.models")
hub_cache = importlib.import_module("spark_pulse.tools.hub_cache")

from spark_pulse.tools.ssh import (  # noqa: E402
    OpenSSHClient,
    SSHClient,
    SSHError,
    SSHErrorType,
    SSHResult,
)

#: The flags the real client uses, mirrored here so the double transfers the
#: way production does. ``test_the_rsync_invocation_is_the_one_we_documented``
#: pins them against :meth:`OpenSSHClient.copy_dir` so this copy cannot drift.
RSYNC_FLAGS = ["-a", "-W", "--partial"]

requires_rsync = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="rsync is not installed"
)


# ── A transport that really runs things, in a sandbox per node ───────────────


class LoopbackSSHClient(SSHClient):
    """SSH double backed by a real directory tree per node.

    A node's copy of the hub lives at ``<node root><the control node's path>``,
    so the paths the production code computes are used verbatim and only the
    root differs. Commands run through ``sh`` after that rewrite, which means
    the ``mkdir``, the verifier, and the rename that publishes a replica are
    the real ones rather than assertions about strings.
    """

    def __init__(self, roots: dict[str, Path], hub: Path):
        self.roots = {node: Path(root) for node, root in roots.items()}
        self.hub = str(hub)
        self.execs: list[tuple[str, str]] = []
        self.copies: list[tuple[str, str, str]] = []
        self.copy_dirs: list[tuple[str, str, str]] = []
        #: Set to raise part-way through a transfer, leaving staging half-full.
        self.interrupt_after: int | None = None
        self.transfer_delay = 0.0

    # -- helpers ------------------------------------------------------------

    def node_path(self, node: str, path: str) -> Path:
        """Where ``path`` lives on ``node``'s filesystem."""
        return self.roots[node] / str(path).lstrip("/")

    def node_hub(self, node: str) -> Path:
        return self.node_path(node, self.hub)

    def node_repo(self, node: str, model_id: str = SAMPLE_MODEL) -> Path:
        return self.node_hub(node) / hub_cache.repo_dir_name(model_id)

    def node_staging(self, node: str, model_id: str = SAMPLE_MODEL) -> Path:
        return (
            self.node_hub(node)
            / models_tool.STAGING_DIRNAME
            / hub_cache.repo_dir_name(model_id)
        )

    def _rewrite(self, node: str, command: str) -> str:
        return command.replace(self.hub, str(self.node_hub(node)))

    # -- SSHClient ----------------------------------------------------------

    def exec(self, host, command, timeout=30, batch_mode=True):
        self.execs.append((host, command))
        proc = subprocess.run(
            ["sh", "-c", self._rewrite(host, command)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return SSHResult(
            returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
        )

    def copy(self, local_path, host, remote_path, timeout=30):
        self.copies.append((local_path, host, remote_path))
        destination = self.node_path(host, remote_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, destination)

    def copy_dir(self, local_dir, host, remote_dir, timeout=60, flags=None):
        self.copy_dirs.append((local_dir, host, remote_dir))
        destination = self.node_path(host, remote_dir)
        destination.mkdir(parents=True, exist_ok=True)
        argv = (
            ["rsync"]
            + (RSYNC_FLAGS if flags is None else flags)
            + [f"{local_dir}/", f"{destination}/"]
        )
        if self.interrupt_after is not None:
            argv = self._partial_argv(argv, local_dir)
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"rsync failed: {result.stderr}")
        # Hold the "transfer" open after the bytes have landed, so a progress
        # poll has a window in which to observe them — a real transfer of these
        # sizes runs for hours.
        if self.transfer_delay:
            time.sleep(self.transfer_delay)
        if self.interrupt_after is not None:
            raise RuntimeError("rsync failed: connection dropped mid-transfer")

    def _partial_argv(self, argv: list[str], local_dir: str) -> list[str]:
        """Transfer only the first ``interrupt_after`` snapshot files.

        rsync's own ``--exclude`` does the cutting, so the files that *do* land
        arrive exactly as a completed transfer would have left them — which is
        what the next run has to be able to reuse.
        """
        names = sorted(SAMPLE_FILES)[self.interrupt_after :]
        excludes: list[str] = []
        for name in names:
            excludes += ["--exclude", f"snapshots/*/{name}"]
            blob = blob_for(Path(local_dir), SAMPLE_COMMIT, name).name
            excludes += ["--exclude", f"blobs/{blob}"]
        return argv[:1] + excludes + argv[1:]


class _RecordingSSHClient(SSHClient):
    """A transport that records and answers, for the paths that never run."""

    def __init__(self, exec_returncode=0, exec_stderr="", exec_error=None, stdout=""):
        self.execs: list[tuple[str, str]] = []
        self.copies: list[tuple[str, str, str]] = []
        self.copy_dirs: list[tuple[str, str, str]] = []
        self._returncode = exec_returncode
        self._stderr = exec_stderr
        self._error = exec_error
        self._stdout = stdout

    def exec(self, host, command, timeout=30, batch_mode=True):
        self.execs.append((host, command))
        if self._error is not None:
            raise self._error
        code = self._returncode
        if callable(code):
            code = code(host)
        stdout = self._stdout(host, command) if callable(self._stdout) else self._stdout
        return SSHResult(
            returncode=code, stdout=stdout, stderr="" if code == 0 else self._stderr
        )

    def copy(self, local_path, host, remote_path, timeout=30):
        self.copies.append((local_path, host, remote_path))

    def copy_dir(self, local_dir, host, remote_dir, timeout=60):
        self.copy_dirs.append((local_dir, host, remote_dir))


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A control node hub holding one complete model. Never the real cache."""
    home = tmp_path / "control" / "hf"
    hub_path = home / "hub"
    hub_path.mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(home))
    sample_entry(hub_path)
    return hub_path


@pytest.fixture
def nodes(tmp_path, hub):
    """Two empty nodes, each a real filesystem of its own."""
    roots = {
        "n1": tmp_path / "node-n1",
        "n2": tmp_path / "node-n2",
    }
    for root in roots.values():
        root.mkdir(parents=True)
    return LoopbackSSHClient(roots, hub)


TOTAL_BYTES = sum(len(v) for v in SAMPLE_FILES.values())


# ── The transfer ─────────────────────────────────────────────────────────────


@requires_rsync
class TestTransfer:
    def test_the_replicated_tree_keeps_its_symlinks(self, hub, nodes):
        result = models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        assert result["ok"] is True

        repo = nodes.node_repo("n1")
        for name in SAMPLE_FILES:
            link = repo / "snapshots" / SAMPLE_COMMIT / name
            assert link.is_symlink(), f"{name} arrived as a copy, not a symlink"
            assert not Path(link.readlink()).is_absolute()
            assert link.resolve().parent == repo / "blobs"

    def test_the_snapshot_resolves_through_its_links_to_the_right_bytes(
        self, hub, nodes
    ):
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        snapshot = nodes.node_repo("n1") / "snapshots" / SAMPLE_COMMIT
        for name, data in SAMPLE_FILES.items():
            assert (snapshot / name).read_bytes() == data

    def test_all_four_directories_travel_together(self, hub, nodes):
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        repo = nodes.node_repo("n1")
        for subdir in hub_cache.CACHE_SUBDIRS:
            assert (repo / subdir).is_dir(), f"{subdir} did not arrive"
        assert (repo / "trees" / f"{SAMPLE_COMMIT}.json").is_file()
        assert (repo / "refs" / "main").read_text() == SAMPLE_COMMIT

    def test_a_copy_that_drops_symlinks_is_partial_not_present(self, hub, nodes):
        """``rsync -r`` skips symlinks, so the snapshot arrives empty.

        The old check ran ``test -d …/snapshots``, which this passes. Presence
        now reads the manifest and calls it what it is.
        """
        staging = (
            str(models_tool._staging_root())
            + f"/{hub_cache.repo_dir_name(SAMPLE_MODEL)}"
        )
        nodes.exec("n1", f"mkdir -p {models_tool._staging_root()}")
        nodes.copy_dir(
            str(hub / hub_cache.repo_dir_name(SAMPLE_MODEL)),
            "n1",
            staging,
            flags=["-r"],
        )
        nodes.exec(
            "n1",
            f"mv {staging} {hub}/{hub_cache.repo_dir_name(SAMPLE_MODEL)}",
        )

        node_repo = nodes.node_repo("n1")
        assert (node_repo / "snapshots").is_dir(), "the old check would pass here"
        report = models_tool.presence(SAMPLE_MODEL, ["n1"], client=nodes)
        row = report["nodes"][0]
        assert row["state"] == hub_cache.STATE_PARTIAL
        assert row["present"] is False
        assert row["missing_count"] == len(SAMPLE_FILES)

    def test_progress_is_bytes_done_against_bytes_expected(
        self, hub, nodes, monkeypatch
    ):
        monkeypatch.setattr(models_tool, "REPLICATION_POLL_INTERVAL", 0.01)
        nodes.transfer_delay = 0.15
        seen: list[dict] = []

        result = models_tool.replicate_to_nodes(
            SAMPLE_MODEL, ["n1"], client=nodes, on_progress=seen.append
        )

        # ``bytes_total`` is the whole entry — weights plus the manifest and
        # refs that travel with them — because that is what lands on the node
        # and what a progress bar has to divide by.
        expected = models_tool.hub_cache.tree_bytes(
            str(hub / hub_cache.repo_dir_name(SAMPLE_MODEL))
        )["bytes"]
        assert result["bytes_total"] == expected > TOTAL_BYTES
        assert result["manifest_bytes"] == TOTAL_BYTES
        assert result["results"][0]["bytes_done"] == expected
        assert result["results"][0]["bytes_verified"] == TOTAL_BYTES
        assert seen, "no progress was reported during the transfer"
        assert all(update["bytes_total"] == expected for update in seen)
        assert all(0 < update["bytes_done"] <= expected for update in seen)

    def test_each_node_reports_its_own_result(self, hub, nodes):
        result = models_tool.replicate_to_nodes(
            SAMPLE_MODEL, ["n1", "n2"], client=nodes
        )
        assert [r["node"] for r in result["results"]] == ["n1", "n2"]
        assert all(r["ok"] and r["published"] for r in result["results"])
        assert all(r["state"] == hub_cache.STATE_VERIFIED for r in result["results"])


# ── Verify, then publish ─────────────────────────────────────────────────────


@requires_rsync
class TestVerifyBeforePublish:
    def test_a_truncated_blob_is_caught_and_nothing_is_published(self, hub, nodes):
        """The defect, end to end: the old code published this and moved on."""
        original = models_tool._replicate_one

        def _truncate_then_verify(**kwargs):
            # Corrupt the staged copy after the transfer but before the
            # verification, which is exactly what a dropped connection or a
            # full disk does on the node.
            def _copy_dir(local_dir, host, remote_dir, timeout=60):
                LoopbackSSHClient.copy_dir(nodes, local_dir, host, remote_dir, timeout)
                staged = nodes.node_staging(host)
                truncate(
                    blob_for(staged, SAMPLE_COMMIT, "model-00002-of-00002.safetensors"),
                    17,
                )

            with patch.object(nodes, "copy_dir", _copy_dir):
                return original(**kwargs)

        with patch.object(models_tool, "_replicate_one", _truncate_then_verify):
            result = models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)

        row = result["results"][0]
        assert result["ok"] is False
        assert row["ok"] is False
        assert row["published"] is False
        assert row["state"] == hub_cache.STATE_PARTIAL
        assert "verification failed" in row["error"]
        assert not nodes.node_repo("n1").exists(), "a bad copy must never publish"

    def test_a_failed_verification_leaves_staging_for_the_next_run(self, hub, nodes):
        original = models_tool._replicate_one

        def _break_then_verify(**kwargs):
            def _copy_dir(local_dir, host, remote_dir, timeout=60):
                LoopbackSSHClient.copy_dir(nodes, local_dir, host, remote_dir, timeout)
                truncate(
                    blob_for(nodes.node_staging(host), SAMPLE_COMMIT, "tokenizer.json"),
                    1,
                )

            with patch.object(nodes, "copy_dir", _copy_dir):
                return original(**kwargs)

        with patch.object(models_tool, "_replicate_one", _break_then_verify):
            models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)

        assert nodes.node_staging("n1").is_dir()

    def test_the_rename_is_what_publishes_it(self, hub, nodes):
        """Before the rename the entry does not exist at its final path."""
        seen: dict[str, bool] = {}
        real_exec = nodes.exec

        def _watch(host, command, timeout=30, batch_mode=True):
            if command.startswith("set -e; rm -rf "):
                seen["existed_before_publish"] = nodes.node_repo(host).exists()
                seen["staging_before_publish"] = nodes.node_staging(host).is_dir()
            return real_exec(host, command, timeout, batch_mode)

        with patch.object(nodes, "exec", _watch):
            models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)

        assert seen["staging_before_publish"] is True
        assert seen["existed_before_publish"] is False
        assert nodes.node_repo("n1").is_dir()
        assert not nodes.node_staging("n1").exists()

    def test_publishing_over_an_older_copy_leaves_no_debris(self, hub, nodes):
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes, force=True)

        node_hub = nodes.node_hub("n1")
        leftovers = [p.name for p in node_hub.iterdir() if ".sp-replaced" in p.name]
        assert leftovers == []
        assert (
            models_tool.presence(SAMPLE_MODEL, ["n1"], client=nodes)["nodes"][0][
                "state"
            ]
            == hub_cache.STATE_VERIFIED
        )

    def test_the_completion_marker_records_commit_bytes_and_time(self, hub, nodes):
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)

        marker = hub_cache.read_marker(str(nodes.node_repo("n1")))
        assert marker["revision"] == SAMPLE_COMMIT
        assert marker["model"] == SAMPLE_MODEL
        assert marker["bytes"] == TOTAL_BYTES
        assert marker["files"] == len(SAMPLE_FILES)
        assert marker["evidence"] == hub_cache.EVIDENCE_MANIFEST
        assert marker["verified_at"]

    def test_an_interrupted_run_leaves_no_marker(self, hub, nodes):
        nodes.interrupt_after = 2
        result = models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)

        assert result["ok"] is False
        assert hub_cache.read_marker(str(nodes.node_staging("n1"))) is None
        assert not nodes.node_repo("n1").exists()

    def test_replication_refuses_a_local_copy_that_does_not_verify(self, hub, nodes):
        truncate(
            blob_for(
                hub / hub_cache.repo_dir_name(SAMPLE_MODEL),
                SAMPLE_COMMIT,
                "config.json",
            ),
            2,
        )
        with pytest.raises(ValueError, match="is partial"):
            models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        assert nodes.copy_dirs == []


# ── Resume ───────────────────────────────────────────────────────────────────


@requires_rsync
class TestResume:
    def test_an_interrupted_transfer_resumes_rather_than_restarting(self, hub, nodes):
        nodes.interrupt_after = 2
        first = models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        assert first["ok"] is False

        staging = nodes.node_staging("n1")
        landed = {
            path.name: path.stat().st_mtime_ns
            for path in sorted((staging / "blobs").iterdir())
        }
        assert landed, "the interrupted run should have left something behind"
        assert len(landed) < len(SAMPLE_FILES)

        nodes.interrupt_after = None
        second = models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)

        assert second["ok"] is True
        published = nodes.node_repo("n1") / "blobs"
        # The blobs from the first run were reused, not re-sent: rsync leaves a
        # file it decides is already correct exactly as it found it.
        for name, mtime in landed.items():
            assert (
                published / name
            ).stat().st_mtime_ns == mtime, (
                f"{name} was transferred again instead of being resumed"
            )
        assert len(list(published.iterdir())) == len(SAMPLE_FILES)

    def test_a_node_that_already_verifies_is_skipped(self, hub, nodes):
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        transfers = len(nodes.copy_dirs)

        again = models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)

        assert again["ok"] is True
        assert again["results"][0]["skipped"] is True
        assert len(nodes.copy_dirs) == transfers, "nothing should have been re-sent"

    def test_force_re_sends_even_a_verified_node(self, hub, nodes):
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        transfers = len(nodes.copy_dirs)

        again = models_tool.replicate_to_nodes(
            SAMPLE_MODEL, ["n1"], client=nodes, force=True
        )

        assert again["results"][0]["skipped"] is False
        assert len(nodes.copy_dirs) == transfers + 1


# ── Presence ─────────────────────────────────────────────────────────────────


@requires_rsync
class TestPresence:
    def test_absent_partial_and_verified_are_distinguished(self, hub, nodes):
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        # n2 gets everything but one shard.
        nodes.interrupt_after = 3
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n2"], client=nodes)
        nodes.exec(
            "n2",
            f"mv {models_tool._staging_root()}/{hub_cache.repo_dir_name(SAMPLE_MODEL)}"
            f" {hub}/{hub_cache.repo_dir_name(SAMPLE_MODEL)}",
        )
        nodes.roots["n3"] = nodes.roots["n1"].parent / "node-n3"
        nodes.roots["n3"].mkdir()

        report = models_tool.presence(SAMPLE_MODEL, ["n1", "n2", "n3"], client=nodes)

        by_node = {row["node"]: row for row in report["nodes"]}
        assert by_node["n1"]["state"] == hub_cache.STATE_VERIFIED
        assert by_node["n2"]["state"] == hub_cache.STATE_PARTIAL
        assert by_node["n3"]["state"] == hub_cache.STATE_ABSENT

    def test_partial_names_what_is_missing(self, hub, nodes):
        nodes.interrupt_after = 3
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        nodes.exec(
            "n1",
            f"mv {models_tool._staging_root()}/{hub_cache.repo_dir_name(SAMPLE_MODEL)}"
            f" {hub}/{hub_cache.repo_dir_name(SAMPLE_MODEL)}",
        )

        row = models_tool.presence(SAMPLE_MODEL, ["n1"], client=nodes)["nodes"][0]

        assert row["state"] == hub_cache.STATE_PARTIAL
        assert row["missing"] == ["tokenizer.json"]
        assert row["missing_count"] == 1
        assert row["files_present"] == len(SAMPLE_FILES) - 1
        assert row["bytes_expected"] == TOTAL_BYTES
        assert 0 < row["bytes_present"] < TOTAL_BYTES

    def test_a_verified_node_reports_when_it_was_verified(self, hub, nodes):
        models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        row = models_tool.presence(SAMPLE_MODEL, ["n1"], client=nodes)["nodes"][0]
        assert row["present"] is True
        assert row["verified_at"]
        assert row["revision"] == SAMPLE_COMMIT

    def test_the_local_verdict_is_a_verification_not_a_directory_listing(
        self, hub, nodes
    ):
        report = models_tool.presence(SAMPLE_MODEL, [], client=nodes)
        assert report["local"] is True
        assert report["local_state"] == hub_cache.STATE_VERIFIED

        truncate(
            blob_for(
                hub / hub_cache.repo_dir_name(SAMPLE_MODEL),
                SAMPLE_COMMIT,
                "model-00001-of-00002.safetensors",
            ),
            3,
        )
        broken = models_tool.presence(SAMPLE_MODEL, [], client=nodes)
        assert (hub / hub_cache.repo_dir_name(SAMPLE_MODEL) / "snapshots").is_dir()
        assert broken["local"] is False
        assert broken["local_state"] == hub_cache.STATE_PARTIAL


class TestPresenceTransport:
    def test_a_transport_failure_is_an_error_not_an_absence(self, hub):
        client = _RecordingSSHClient(
            exec_error=SSHError(
                error_type=SSHErrorType.NETWORK, host="n1", message="no route to host"
            )
        )
        row = models_tool.presence(SAMPLE_MODEL, ["n1"], client=client)["nodes"][0]
        assert row["state"] == hub_cache.STATE_ABSENT
        assert row["present"] is False
        assert "no route to host" in row["error"]

    def test_an_unparseable_report_is_not_read_as_success(self, hub):
        client = _RecordingSSHClient(stdout="command not found: python3")
        row = models_tool.presence(SAMPLE_MODEL, ["n1"], client=client)["nodes"][0]
        assert row["present"] is False
        assert row["reason"] == "no verification report"


# ── Credentials ──────────────────────────────────────────────────────────────


class TestTokenStaysOnTheControlNode:
    TOKEN = "hf_TOPSECRETtokenvalue0123456789"

    @requires_rsync
    def test_no_command_or_file_sent_to_a_node_carries_the_token(
        self, hub, nodes, monkeypatch
    ):
        monkeypatch.setenv("HF_TOKEN", self.TOKEN)

        result = models_tool.replicate_to_nodes(
            SAMPLE_MODEL, ["n1", "n2"], client=nodes
        )
        assert result["ok"] is True

        for _host, command in nodes.execs:
            assert self.TOKEN not in command
        for local_path, _host, _remote in nodes.copies:
            assert self.TOKEN not in Path(local_path).read_text()
        for root in nodes.roots.values():
            for path in root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    assert self.TOKEN.encode() not in path.read_bytes()

    @requires_rsync
    def test_every_node_result_says_no_token_was_sent(self, hub, nodes):
        result = models_tool.replicate_to_nodes(SAMPLE_MODEL, ["n1"], client=nodes)
        assert result["results"][0]["token_sent"] is False

    def test_worker_env_strips_the_token_and_pins_the_hub_offline(self):
        env = models_tool.worker_env(
            {"HF_TOKEN": self.TOKEN, "NCCL_SOCKET_IFNAME": "eth0"}
        )
        assert "HF_TOKEN" not in env
        assert env["HF_HUB_OFFLINE"] == "1"
        assert env["TRANSFORMERS_OFFLINE"] == "1"
        assert env["NCCL_SOCKET_IFNAME"] == "eth0", "unrelated variables survive"

    def test_worker_env_strips_every_spelling_of_the_token(self):
        env = models_tool.worker_env(
            {key: self.TOKEN for key in models_tool.TOKEN_ENV_KEYS}
        )
        assert not any(key in env for key in models_tool.TOKEN_ENV_KEYS)

    def test_worker_env_does_not_mutate_its_argument(self):
        original = {"HF_TOKEN": self.TOKEN}
        models_tool.worker_env(original)
        assert original == {"HF_TOKEN": self.TOKEN}


# ── The hub's own tooling ────────────────────────────────────────────────────


class TestHubCliCrossCheck:
    """``hf cache verify`` runs on the control node, and only there.

    It fetches the revision's file list from the hub, so it needs the network
    and — for a gated repo — the token. A worker node has neither by design,
    which is why the node-side check reads the manifest the download already
    cached instead.
    """

    def test_a_clean_cli_run_leaves_the_verdict_verified(self, hub):
        completed = subprocess.CompletedProcess([], 0, '{"mismatches": []}', "")
        with patch("shutil.which", return_value="/usr/bin/hf"):
            with patch("subprocess.run", return_value=completed) as run:
                report = models_tool.verify_local(SAMPLE_MODEL, use_cli=True)

        argv = run.call_args[0][0]
        assert argv[:3] == ["hf", "cache", "verify"]
        assert "--cache-dir" in argv and str(hub) in argv
        assert argv[argv.index("--revision") + 1] == SAMPLE_COMMIT
        assert report["state"] == hub_cache.STATE_VERIFIED
        assert report["hub_cli"]["state"] == hub_cache.STATE_VERIFIED

    def test_a_cli_mismatch_downgrades_the_verdict(self, hub):
        completed = subprocess.CompletedProcess([], 1, "", "checksum mismatch: x.bin")
        with patch("shutil.which", return_value="/usr/bin/hf"):
            with patch("subprocess.run", return_value=completed):
                report = models_tool.verify_local(SAMPLE_MODEL, use_cli=True)

        assert report["state"] == hub_cache.STATE_PARTIAL
        assert "checksum mismatch" in report["reason"]

    def test_a_missing_cli_is_not_a_failure(self, hub):
        with patch("shutil.which", return_value=None):
            report = models_tool.verify_local(SAMPLE_MODEL, use_cli=True)
        assert report["state"] == hub_cache.STATE_VERIFIED
        assert report["hub_cli"]["state"] == "unavailable"

    def test_the_cli_is_never_asked_by_default(self, hub):
        with patch("subprocess.run") as run:
            report = models_tool.verify_local(SAMPLE_MODEL)
        assert run.call_count == 0
        assert "hub_cli" not in report


# ── The transport contract ───────────────────────────────────────────────────


class TestTransportContract:
    def test_the_rsync_invocation_is_the_one_we_documented(self):
        """``-a`` implies ``-l``; ``-W`` and ``--partial``; never ``-z``.

        ``-a`` is what preserves the relative symlinks that make the tree
        movable — a copy made with ``-r`` skips them and leaves an empty
        snapshot. ``-W`` skips the delta algorithm, which only burns CPU on
        immutable content-addressed blobs over a fast link. ``--partial`` keeps
        the bytes of an interrupted multi-hour transfer. Compression is absent
        on purpose: safetensors do not compress and ``-z`` would make the CPU
        the bottleneck.
        """
        client = OpenSSHClient(user="ubuntu", multiplex=False)
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            with patch("shutil.which", return_value="/usr/bin/rsync"):
                client.copy_dir("/local/repo", "n1", "/remote/repo")
        argv = run.call_args[0][0]

        assert argv[0] == "rsync"
        assert set(RSYNC_FLAGS) <= set(argv)
        assert "-z" not in argv and "--compress" not in argv
        assert argv[-2:] == ["/local/repo/", "ubuntu@n1:/remote/repo/"]

    def test_replication_goes_through_the_ssh_client_not_a_subprocess(self, hub):
        """No shelling out: the transport is the one phase A fixed."""
        client = _RecordingSSHClient(stdout="")
        with patch("subprocess.run") as run:
            models_tool.presence(SAMPLE_MODEL, ["n1"], client=client)
        assert run.call_count == 0
        assert client.execs and client.copies

    def test_the_verifier_is_shipped_to_the_node_before_it_is_run(self, hub):
        client = _RecordingSSHClient(stdout="")
        models_tool.presence(SAMPLE_MODEL, ["n1"], client=client)
        assert client.copies[0][0] == hub_cache.__file__
        assert client.copies[0][2] == models_tool._remote_helper_path()

    def test_the_default_client_is_the_strict_openssh_one(self):
        client = models_tool._make_ssh_client("ubuntu")
        assert isinstance(client, OpenSSHClient)
        assert client.host_key_policy == "strict"

    def test_sync_to_nodes_is_the_old_name_for_replication(self):
        assert models_tool.sync_to_nodes is models_tool.replicate_to_nodes

    def test_replication_refuses_an_uncached_model(self, hub):
        with pytest.raises(ValueError, match="not in local cache"):
            models_tool.replicate_to_nodes("acme/missing", ["n1"])

    def test_replication_refuses_an_empty_node_list(self, hub):
        with pytest.raises(ValueError, match="No nodes specified"):
            models_tool.replicate_to_nodes(SAMPLE_MODEL, [])

    def test_a_publish_command_quotes_every_path(self):
        command = models_tool._publish_command("/a b/staging", "/a b/final")
        assert "'/a b/staging'" in command
        assert "'/a b/final'" in command

    def test_the_verify_command_carries_the_marker_as_one_argument(self):
        command = models_tool._remote_verify_command(
            "/hub/models--x--y",
            SAMPLE_COMMIT,
            require_manifest=True,
            deep=True,
            marker={"model": "x/y"},
        )
        assert "--require-manifest" in command
        assert "--deep" in command
        assert json.dumps({"model": "x/y"}, sort_keys=True) in command
