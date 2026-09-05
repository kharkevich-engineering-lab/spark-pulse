"""Coverage for the untested half of ``spark_pulse.tools.system``.

``tests/test_tools_system.py`` covers the nvidia-smi/free/df happy paths; this
file covers the process-tracking helpers (/proc, cgroups, ``docker inspect``),
``kill_gpu_process`` and the malformed-input and missing-command failure paths.

The module reads ``/proc`` through the ``Path`` name it imports, so the
``fake_proc`` fixture swaps that name for a factory rooted in tmp_path — the
parsers then run against real files holding real ``/proc`` text.

Usage:
    pytest tests/test_tools_system_coverage.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# The real module. Neither ``from spark_pulse.tools import system`` nor
# ``import spark_pulse.tools.system as system`` reaches it: both read the
# attribute on the tools package, which under SIMULATION_MODE is the mock (and
# the mock delegates only a subset of these). sys.modules holds the submodule
# itself — the idiom tests/conftest.py uses for the same reason.
import spark_pulse.tools.system  # noqa: F401

system = sys.modules["spark_pulse.tools.system"]

# ── /proc fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def fake_proc(tmp_path, monkeypatch):
    """Root the module's ``/proc`` reads in tmp_path.

    Returns a writer: ``proc(pid, cgroup=..., cmdline=..., ppid=...)``.
    """
    root = tmp_path / "fakeroot"

    def _path(p) -> Path:
        text = str(p)
        if text.startswith("/proc"):
            return root / text.lstrip("/")
        return Path(text)

    monkeypatch.setattr(system, "Path", _path)

    def write(pid, *, cgroup=None, cmdline=None, ppid=None, status=None):
        d = root / "proc" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        if cgroup is not None:
            (d / "cgroup").write_text(cgroup)
        if cmdline is not None:
            (d / "cmdline").write_text("\x00".join(cmdline) + "\x00")
        if ppid is not None:
            (d / "status").write_text(
                f"Name:\tpython3\nUmask:\t0022\nState:\tS (sleeping)\n"
                f"Tgid:\t{pid}\nPid:\t{pid}\nPPid:\t{ppid}\n"
            )
        if status is not None:
            (d / "status").write_text(status)
        return d

    (root / "proc").mkdir(parents=True)
    write.root = root
    return write


# Real cgroup v2 text from a container started by dockerd under systemd.
CGROUP_DOCKER = (
    "0::/system.slice/docker-"
    "3f1c9a2b4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8.scope\n"
)
CGROUP_HOST = "0::/user.slice/user-1000.slice/session-3.scope\n"


class TestCgroupContainerId:
    def test_extracts_the_short_id_from_a_docker_scope(self, fake_proc):
        fake_proc(4321, cgroup=CGROUP_DOCKER)

        assert system._cgroup_container_id(4321) == "3f1c9a2b4d5e"

    def test_returns_none_for_a_process_outside_a_container(self, fake_proc):
        fake_proc(4321, cgroup=CGROUP_HOST)

        assert system._cgroup_container_id(4321) is None

    def test_returns_none_when_the_process_is_gone(self, fake_proc):
        assert system._cgroup_container_id(999999) is None


class TestProcChildren:
    def test_maps_parents_to_direct_children(self, fake_proc):
        fake_proc(1, ppid=0)
        fake_proc(100, ppid=1)
        fake_proc(101, ppid=1)
        fake_proc(200, ppid=100)

        children = system._proc_children()

        assert sorted(children[1]) == [100, 101]
        assert children[100] == [200]
        assert 200 not in children

    def test_ignores_non_pid_entries_and_unreadable_processes(self, fake_proc):
        fake_proc(100, ppid=1)
        (fake_proc.root / "proc" / "self").mkdir()
        (fake_proc.root / "proc" / "meminfo").write_text("MemTotal: 1 kB\n")
        fake_proc(101)  # a pid directory with no status file (exited mid-scan)

        assert system._proc_children() == {1: [100]}


class TestDescendants:
    def test_walks_the_whole_subtree(self):
        children = {1: [2, 3], 2: [4], 4: [5]}

        assert sorted(system._descendants(1, children)) == [2, 3, 4, 5]

    def test_stops_at_max_depth(self):
        children = {1: [2], 2: [3], 3: [4]}

        assert sorted(system._descendants(1, children, max_depth=2)) == [2, 3]

    def test_tolerates_cycles_and_leaf_roots(self):
        assert system._descendants(1, {1: [2], 2: [1]}) == [2]
        assert system._descendants(7, {}) == []


class TestDockerContainerName:
    def test_reads_the_name_flag_of_a_docker_run(self, fake_proc):
        fake_proc(
            500,
            cmdline=[
                "/usr/bin/docker",
                "run",
                "--rm",
                "--name",
                "vllm-qwen",
                "img:tag",
            ],
        )

        assert system._docker_container_name(500) == "vllm-qwen"

    def test_reads_the_target_of_a_docker_exec_skipping_flags(self, fake_proc):
        fake_proc(501, cmdline=["docker", "exec", "-it", "vllm-qwen", "bash"])

        assert system._docker_container_name(501) == "vllm-qwen"

    def test_reads_the_target_of_docker_start(self, fake_proc):
        fake_proc(502, cmdline=["docker", "start", "vllm-qwen"])

        assert system._docker_container_name(502) == "vllm-qwen"

    def test_returns_none_for_a_non_docker_process(self, fake_proc):
        fake_proc(503, cmdline=["/usr/bin/python3", "-m", "vllm.entrypoints"])

        assert system._docker_container_name(503) is None

    def test_returns_none_for_a_docker_command_naming_no_container(self, fake_proc):
        fake_proc(504, cmdline=["docker", "ps", "-a"])
        fake_proc(505, cmdline=["docker", "exec"])  # truncated

        assert system._docker_container_name(504) is None
        assert system._docker_container_name(505) is None

    def test_returns_none_for_an_empty_or_missing_cmdline(self, fake_proc):
        fake_proc(506, cmdline=[])

        assert system._docker_container_name(506) is None
        assert system._docker_container_name(999999) is None


class TestResolveContainerName:
    def test_truncates_the_inspected_id_to_twelve_chars(self, monkeypatch):
        full = "3f1c9a2b4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8"
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=full + "\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert system._resolve_container_name("vllm-qwen") == "3f1c9a2b4d5e"
        assert calls == [["docker", "inspect", "--format", "{{.Id}}", "vllm-qwen"]]

    def test_returns_none_when_the_container_is_unknown(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **_k: subprocess.CompletedProcess(cmd, 1, "", "No such object"),
        )

        assert system._resolve_container_name("gone") is None

    def test_returns_none_when_docker_is_missing(self, monkeypatch):
        def boom(*_a, **_k):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(subprocess, "run", boom)

        assert system._resolve_container_name("vllm-qwen") is None


class TestEnrichGpuProcessTracking:
    def test_tracks_processes_in_a_container_launched_by_a_deployment(
        self, fake_proc, monkeypatch
    ):
        # deployment pid 100 → child 150 runs `docker exec vllm-qwen ...`
        fake_proc(100, ppid=1)
        fake_proc(150, ppid=100, cmdline=["docker", "exec", "vllm-qwen", "python"])
        # the GPU process itself lives inside that container
        fake_proc(900, cgroup=CGROUP_DOCKER)
        # ...and this one belongs to somebody else's container
        fake_proc(
            901,
            cgroup="0::/system.slice/docker-aaaabbbbcccc0000111122223333.scope\n",
        )

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **_k: subprocess.CompletedProcess(
                cmd,
                0,
                "3f1c9a2b4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8\n",
                "",
            ),
        )

        processes = [{"pid": 900}, {"pid": 901}]
        system.enrich_gpu_process_tracking(processes, [{"pid": 100}])

        assert processes[0]["is_tracked"] is True
        assert processes[1]["is_tracked"] is False

    def test_falls_back_to_direct_pid_matching_outside_containers(self, fake_proc):
        fake_proc(900, cgroup=CGROUP_HOST)
        fake_proc(901, cgroup=CGROUP_HOST)

        processes = [{"pid": 900}, {"pid": 901}]
        system.enrich_gpu_process_tracking(processes, [{"pid": 900}, {"pid": None}, {}])

        assert processes[0]["is_tracked"] is True
        assert processes[1]["is_tracked"] is False

    def test_is_a_no_op_without_gpu_processes(self, fake_proc):
        processes: list[dict] = []

        system.enrich_gpu_process_tracking(processes, [{"pid": 100}])

        assert processes == []

    def test_still_falls_back_when_proc_scanning_blows_up(self, fake_proc, monkeypatch):
        fake_proc(900, cgroup=CGROUP_HOST)

        def boom():
            raise PermissionError("/proc")

        monkeypatch.setattr(system, "_proc_children", boom)

        processes = [{"pid": 900}]
        system.enrich_gpu_process_tracking(processes, [{"pid": 900}])

        assert processes[0]["is_tracked"] is True


# ── nvidia-smi parsing edge cases ────────────────────────────────────────────


def _stdout(text):
    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

    return fake_run


class TestGpuStatsEdgeCases:
    def test_skips_blank_and_truncated_lines(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            _stdout(
                "\n"
                "0, GPU-123, NVIDIA GB10\n"  # truncated row
                "1, GPU-456, NVIDIA GB10, 131072, 4096, 126976, 40, 3, 12.5, 240\n"
            ),
        )

        gpus = system.get_gpu_stats()

        assert [g["index"] for g in gpus] == [1]
        assert gpus[0]["memory_free"] == 126976

    def test_returns_empty_when_no_command_yields_a_gpu(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _stdout("\n"))

        assert system.get_gpu_stats() == []

    def test_returns_empty_when_nvidia_smi_always_fails(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **_k: subprocess.CompletedProcess(
                cmd, 9, "", "NVIDIA-SMI has failed"
            ),
        )

        assert system.get_gpu_stats() == []

    def test_returns_empty_on_unparseable_numbers(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            _stdout("zero, GPU-123, NVIDIA GB10, 131072, 4096, 126976, 40, 3\n"),
        )

        assert system.get_gpu_stats() == []

    def test_times_out_gracefully(self, monkeypatch):
        def boom(cmd, **_k):
            raise subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(subprocess, "run", boom)

        assert system.get_gpu_stats() == []


class TestGpuProcessStatsEdgeCases:
    def test_returns_empty_when_nvidia_smi_exits_non_zero(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **_k: subprocess.CompletedProcess(cmd, 9, "", "failed"),
        )

        assert system.get_gpu_process_stats() == []

    def test_skips_blank_short_and_unparseable_rows(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            _stdout(
                "GPU-123, 98251\n"  # short row
                "\n"  # blank row between records
                "GPU-123, [N/A], python3, 512 MiB\n"  # unparseable pid
                "GPU-123, 98252, VLLM::EngineCore, 83421 MiB\n"
            ),
        )

        processes = system.get_gpu_process_stats()

        assert processes == [
            {
                "gpu_uuid": "GPU-123",
                "pid": 98252,
                "process_name": "VLLM::EngineCore",
                "used_memory": 83421,
            }
        ]

    def test_returns_empty_when_nvidia_smi_is_missing(self, monkeypatch):
        def boom(*_a, **_k):
            raise FileNotFoundError("nvidia-smi")

        monkeypatch.setattr(subprocess, "run", boom)

        assert system.get_gpu_process_stats() == []


class TestCpuAndDiskEdgeCases:
    def test_cpu_stats_default_when_free_reports_no_mem_line(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _stdout("Swap: 0 0 0\n"))

        assert system.get_cpu_stats() == {
            "total": 0,
            "used": 0,
            "free": 0,
            "available": 0,
            "usage_percent": 0,
        }

    def test_cpu_stats_falls_back_to_free_without_an_available_column(
        self, monkeypatch
    ):
        monkeypatch.setattr(subprocess, "run", _stdout("Mem: 1000 400 600\n"))

        cpu = system.get_cpu_stats()

        assert cpu["available"] == 600
        assert cpu["usage_percent"] == 40.0

    def test_cpu_stats_avoids_dividing_by_a_zero_total(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _stdout("Mem: 0 0 0 0 0 0\n"))

        assert system.get_cpu_stats()["usage_percent"] == 0

    def test_disk_stats_skips_short_lines(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            _stdout(
                "Filesystem 1B-blocks Used Available Use% Mounted on\n"
                "/dev/nvme0n1p2\n"
                "/dev/nvme0n1p2 1000 400 600 40% /home\n"
            ),
        )

        disks = system.get_disk_stats()

        assert [d["mount"] for d in disks] == ["/home"]
        assert disks[0]["free"] == 600

    def test_disk_stats_returns_empty_when_df_fails(self, monkeypatch):
        def boom(*_a, **_k):
            raise subprocess.SubprocessError("df")

        monkeypatch.setattr(subprocess, "run", boom)

        assert system.get_disk_stats() == []


# ── kill_gpu_process ─────────────────────────────────────────────────────────


class TestKillGpuProcess:
    def test_stops_the_containing_docker_container(self, fake_proc, monkeypatch):
        fake_proc(900, cgroup=CGROUP_DOCKER)
        calls = []

        def fake_run(cmd, **_k):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "3f1c9a2b4d5e\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert system.kill_gpu_process(900) == {
            "killed": True,
            "pid": 900,
            "method": "docker_stop",
            "container": "3f1c9a2b4d5e",
        }
        assert calls == [["docker", "stop", "3f1c9a2b4d5e"]]

    def test_reports_the_docker_error_when_the_stop_fails(self, fake_proc, monkeypatch):
        fake_proc(900, cgroup=CGROUP_DOCKER)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **_k: subprocess.CompletedProcess(
                cmd, 1, "", "Error response from daemon: no such container\n"
            ),
        )

        assert system.kill_gpu_process(900) == {
            "killed": False,
            "pid": 900,
            "error": "Error response from daemon: no such container",
        }

    def test_reports_a_generic_error_when_docker_is_silent(
        self, fake_proc, monkeypatch
    ):
        fake_proc(900, cgroup=CGROUP_DOCKER)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **_k: subprocess.CompletedProcess(cmd, 1, "", "  \n"),
        )

        assert system.kill_gpu_process(900)["error"] == "docker stop failed"

    def test_sends_sigterm_to_a_bare_process(self, fake_proc, monkeypatch):
        import signal

        fake_proc(900, cgroup=CGROUP_HOST)
        signals = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

        assert system.kill_gpu_process(900) == {
            "killed": True,
            "pid": 900,
            "method": "sigterm",
        }
        assert signals == [(900, signal.SIGTERM)]

    def test_reports_a_process_that_already_exited(self, fake_proc, monkeypatch):
        fake_proc(900, cgroup=CGROUP_HOST)

        def boom(*_a):
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", boom)

        assert system.kill_gpu_process(900) == {
            "killed": False,
            "pid": 900,
            "error": "Process not found",
        }

    def test_reports_a_process_owned_by_another_user(self, fake_proc, monkeypatch):
        fake_proc(900, cgroup=CGROUP_HOST)

        def boom(*_a):
            raise PermissionError

        monkeypatch.setattr(os, "kill", boom)

        assert system.kill_gpu_process(900) == {
            "killed": False,
            "pid": 900,
            "error": "Permission denied",
        }


class TestGetAllMemory:
    def test_aggregates_the_four_collectors(self, monkeypatch):
        monkeypatch.setattr(system, "get_gpu_stats", lambda: [{"index": 0}])
        monkeypatch.setattr(system, "get_cpu_stats", lambda: {"total": 131072})
        monkeypatch.setattr(system, "get_disk_stats", lambda: [{"mount": "/"}])
        monkeypatch.setattr(system, "get_gpu_process_stats", lambda: [{"pid": 900}])

        assert system.get_all_memory() == {
            "gpu": [{"index": 0}],
            "cpu": {"total": 131072},
            "disk": [{"mount": "/"}],
            "processes": [{"pid": 900}],
        }
