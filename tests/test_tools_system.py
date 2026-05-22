import subprocess
from types import SimpleNamespace

from spark_pulse.tools import system


def test_get_gpu_stats_parses_nvidia_smi(monkeypatch):
    output = "0, GPU-123, NVIDIA A100, 40960, 20480, 20480, 45, 75, 10.5, 40\n"

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(subprocess, "run", fake_run)

    gpus = system.get_gpu_stats()

    assert len(gpus) == 1
    assert gpus[0]["index"] == 0
    assert gpus[0]["gpu"] == "GPU 0"
    assert gpus[0]["uuid"] == "GPU-123"
    assert gpus[0]["name"] == "NVIDIA A100"
    assert gpus[0]["memory_total"] == 40960
    assert gpus[0]["utilization"] == 75
    assert gpus[0]["power_draw"] == 10.5
    assert gpus[0]["power_limit"] == 40


def test_get_gpu_stats_parses_without_power_fields(monkeypatch):
    output = "0, GPU-123, NVIDIA A100, 40960, 20480, 20480, 45, 75\n"

    def fake_run(*args, **kwargs):
        if args[0][1].startswith("--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,temperature.gpu,utilization.gpu,power.draw,power.limit"):
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(subprocess, "run", fake_run)

    gpus = system.get_gpu_stats()

    assert len(gpus) == 1
    assert gpus[0]["power_draw"] is None
    assert gpus[0]["power_limit"] is None


def test_get_gpu_stats_parses_na_power_fields(monkeypatch):
    output = "0, GPU-123, NVIDIA A100, 40960, 20480, 20480, 45, 75, N/A, N/A\n"

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(subprocess, "run", fake_run)

    gpus = system.get_gpu_stats()

    assert len(gpus) == 1
    assert gpus[0]["power_draw"] is None
    assert gpus[0]["power_limit"] is None


def test_get_cpu_stats_parses_free_output(monkeypatch):
    output = "Mem: 1000 500 200 0 0 300\n"

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(subprocess, "run", fake_run)

    cpu = system.get_cpu_stats()

    assert cpu["total"] == 1000
    assert cpu["used"] == 500
    assert cpu["available"] == 300
    assert cpu["usage_percent"] == 50.0


def test_get_disk_stats_parses_df_output(monkeypatch):
    output = "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/root 100 40 60 40% /\n"

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(subprocess, "run", fake_run)

    disks = system.get_disk_stats()

    assert len(disks) == 1
    assert disks[0]["mount"] == "/"
    assert disks[0]["usage_percent"] == 40.0


def test_get_disk_stats_deduplicates_mounts(monkeypatch):
    output = (
        "Filesystem 1B-blocks Used Available Use% Mounted on\n"
        "/dev/root 100 40 60 40% /\n"
        "/dev/root 100 40 60 40% /\n"
    )

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(subprocess, "run", fake_run)

    disks = system.get_disk_stats()

    assert len(disks) == 1


def test_get_gpu_process_stats_parses_nvidia_smi(monkeypatch):
    output = "GPU-123, 98251, VLLM::EngineCore, 83421 MiB\n"

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(subprocess, "run", fake_run)

    processes = system.get_gpu_process_stats()

    assert len(processes) == 1
    assert processes[0]["gpu_uuid"] == "GPU-123"
    assert processes[0]["pid"] == 98251
    assert processes[0]["used_memory"] == 83421


def test_system_parsers_fallback_on_missing_commands(monkeypatch):
    def raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", raise_not_found)

    assert system.get_gpu_stats() == []
    assert system.get_cpu_stats()["total"] == 0
    assert system.get_disk_stats() == []
