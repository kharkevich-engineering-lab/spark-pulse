from types import SimpleNamespace

from spark_pulse import service


def test_get_service_dir_by_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)

    assert service._get_service_dir("system") == service.Path("/var/lib/spark-pulse")
    assert (
        service._get_service_dir("user")
        == tmp_path / ".local" / "share" / "spark-pulse"
    )


def test_systemctl_cmd_includes_user_flag_for_user_scope():
    assert service._systemctl_cmd(["status", service.SERVICE_NAME], scope="system") == [
        "systemctl",
        "status",
        service.SERVICE_NAME,
    ]
    assert service._systemctl_cmd(["status", service.SERVICE_NAME], scope="user") == [
        "systemctl",
        "--user",
        "status",
        service.SERVICE_NAME,
    ]


def test_render_service_content_user_scope_omits_user_directive(monkeypatch, tmp_path):
    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(service.sys, "executable", "/tmp/venv/bin/python")

    content = service._render_service_content(
        scope="user",
        user="alice",
        work_dir=tmp_path / ".local" / "share" / "spark-pulse",
        env_file=tmp_path / ".config" / "spark-pulse" / "spark-pulse.env",
        host="127.0.0.1",
        port=8100,
        data_dir=tmp_path / "data",
    )

    assert "WantedBy=default.target" in content
    assert "\nUser=" not in content
    assert "ExecStart=/tmp/venv/bin/python -m uvicorn spark_pulse.app:app" in content


def test_install_systemd_user_scope_writes_user_files(monkeypatch, tmp_path):
    package_dir = tmp_path / "package" / "spark_pulse"
    data_dir = package_dir / "data"
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(service, "_get_package_dir", lambda: package_dir)
    monkeypatch.setattr(service.sys, "executable", "/usr/bin/python3")
    monkeypatch.setitem(
        service.config._data, "spark_vllm_path", "/tmp/spark-vllm-docker"
    )

    calls = []

    def fake_run(cmd, check=True):
        calls.append((cmd, check))
        return SimpleNamespace(stdout="active")

    monkeypatch.setattr(service, "_run_cmd", fake_run)

    service.install_systemd(scope="user", start=False)

    unit_file = tmp_path / ".config" / "systemd" / "user" / service.SERVICE_NAME
    env_file = tmp_path / ".config" / "spark-pulse" / "spark-pulse.env"

    assert unit_file.exists()
    assert env_file.exists()
    assert "SPARK_VLLM_PATH=/tmp/spark-vllm-docker" in env_file.read_text()
    assert calls == [
        (["systemctl", "--user", "daemon-reload"], True),
        (["systemctl", "--user", "enable", service.SERVICE_NAME], True),
    ]


def test_get_status_user_scope_uses_systemctl_user(monkeypatch):
    seen = []

    def fake_run(cmd, check=True):
        seen.append((cmd, check))
        return SimpleNamespace(stdout="active\n")

    monkeypatch.setattr(service, "_run_cmd", fake_run)

    assert service.get_status(scope="user") == "active"
    assert seen == [(["systemctl", "--user", "is-active", service.SERVICE_NAME], False)]
