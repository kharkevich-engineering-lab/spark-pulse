"""The container runtime's checks, per runtime, behind the run backend (#168).

The doctor and the pre-flight both read a node's runtime through
``RunBackend.diagnose``; which checks apply is decided here from what the
node's agent reports (``container_runtime``, #166). Each set is pure over the
agent's facts and the output of the commands it declares, so it is tested
with neither a node nor a transport.
"""

from __future__ import annotations

from typing import Any

import pytest

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.tools import preflight
from spark_pulse.tools.backends import DockerBackend
from spark_pulse.tools.backends import docker as docker_backend
from spark_pulse.tools.backends import runtime_checks as rc
from spark_pulse.tools.preflight import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
    ProbeResult,
)


def out(text: str = "", code: int = 0) -> ProbeResult:
    return ProbeResult(reachable=True, returncode=code, stdout=text)


UNREACHABLE = ProbeResult(reachable=False, error="no agent")

PODMAN = rc.RuntimeFacts(
    reported=True,
    name="podman",
    version="4.9.3",
    rootless=True,
    cdi_devices=("nvidia.com/gpu=0", "nvidia.com/gpu=all"),
    docker_version="4.9.3",
)


def answers(table: dict[str, ProbeResult]):
    """A runner answering each declared command by its key's command text."""
    asked: list[str] = []

    def run(command: str) -> ProbeResult:
        asked.append(command)
        return table.get(command, out("", 127))

    run.asked = asked  # type: ignore[attr-defined]
    return run


def by_check(report: rc.RuntimeReport) -> dict[str, rc.RuntimeFinding]:
    return {f.check: f for f in report.findings}


# ── Which checks a node gets ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "facts, expected",
    [
        (PODMAN, "podman"),
        (
            rc.RuntimeFacts(reported=True, name="docker", docker_version="29.2.1"),
            "docker",
        ),
        # Nothing to ask, as in simulation: Docker from the probe, as always.
        (rc.RuntimeFacts(), "docker"),
        # An agent older than the field still reports its daemon's version.
        (rc.RuntimeFacts(reported=True, docker_version="27.5.1"), "docker"),
        # Connected, and its daemon answered nothing.
        (rc.RuntimeFacts(reported=True), "unknown"),
    ],
)
def test_the_checks_follow_what_the_agent_reports(facts, expected):
    assert rc.checks_for(facts).name == expected


# ── Docker ───────────────────────────────────────────────────────────────────


def test_the_agents_daemon_answer_wins_over_the_cli():
    """The agent's connection is the one engines are created on."""
    facts = rc.RuntimeFacts(reported=True, name="docker", docker_version="29.2.1")
    run = answers({rc.DOCKER_COMMAND: out("", 127)})
    found = by_check(rc.run_checks(rc.DockerChecks(), "spark-02", facts, run))
    assert found[rc.DOCKER_DAEMON].status == "ok"
    assert "29.2.1" in found[rc.DOCKER_DAEMON].detail


def test_a_connected_agent_with_no_daemon_is_broken_and_left_to_a_human():
    report = rc.run_checks(
        rc.DockerChecks(),
        "spark-02",
        rc.RuntimeFacts(reported=True, name="docker"),
        lambda _c: UNREACHABLE,
    )
    daemon = by_check(report)[rc.DOCKER_DAEMON]
    assert daemon.status == "broken"
    assert "answered nothing" in daemon.detail
    # Could not ask is not "no GPU path".
    assert by_check(report)[rc.GPU_ACCESS].status == "unknown"


def test_docker_reports_where_it_keeps_images():
    run = answers(
        {
            rc.DOCKER_COMMAND: out("29.2.1|/data/docker|runc"),
            rc.TOOLKIT_COMMAND: out("hook\n"),
        }
    )
    report = rc.run_checks(rc.DockerChecks(), "spark-02", rc.RuntimeFacts(), run)
    assert report.storage_root == "/data/docker"
    assert by_check(report)[rc.GPU_ACCESS].status == "ok"


# ── Podman ───────────────────────────────────────────────────────────────────


def podman_run(**overrides: ProbeResult):
    table = {
        rc.PODMAN_STORE_COMMAND: out("/home/ark/.local/share/containers/storage\n"),
        rc.PODMAN_SOCKET_COMMAND: out("enabled\n"),
        rc.SUBUID_COMMAND: out("/etc/subuid yes\n/etc/subgid yes\n"),
    }
    table.update({getattr(rc, k): v for k, v in overrides.items()})
    return answers(table)


def test_a_healthy_rootless_podman_node():
    run = podman_run()
    report = rc.run_checks(rc.PodmanChecks(), "desk", PODMAN, run)
    found = by_check(report)
    assert report.runtime == "podman"
    assert found[rc.PODMAN_DAEMON].status == "ok"
    assert "rootless" in found[rc.PODMAN_DAEMON].detail
    assert found[rc.GPU_ACCESS].status == "ok"
    assert "nvidia.com/gpu=all" in found[rc.GPU_ACCESS].detail
    assert found[rc.PODMAN_SOCKET].status == "ok"
    assert found[rc.SUBUID].status == "ok"
    assert report.storage_root == "/home/ark/.local/share/containers/storage"
    # Never Docker's CLI: through the shim it cannot render Docker's template.
    assert rc.DOCKER_COMMAND not in run.asked


def test_podman_without_a_cdi_gpu_is_broken_with_the_generate_command():
    facts = rc.RuntimeFacts(
        reported=True, name="podman", version="4.9.3", rootless=True
    )
    gpu = by_check(rc.run_checks(rc.PodmanChecks(), "desk", facts, podman_run()))[
        rc.GPU_ACCESS
    ]
    assert gpu.status == "broken"
    assert "nvidia-ctk cdi generate" in gpu.remedy


def test_a_socket_that_was_only_started_is_a_warning():
    socket = by_check(
        rc.run_checks(
            rc.PodmanChecks(),
            "desk",
            PODMAN,
            podman_run(PODMAN_SOCKET_COMMAND=out("disabled\n")),
        )
    )[rc.PODMAN_SOCKET]
    assert socket.status == "warn"
    assert "systemctl --user enable --now podman.socket" in socket.remedy


def test_missing_subordinate_ids_are_named():
    subuid = by_check(
        rc.run_checks(
            rc.PodmanChecks(),
            "desk",
            PODMAN,
            podman_run(SUBUID_COMMAND=out("/etc/subuid yes\n/etc/subgid no\n")),
        )
    )[rc.SUBUID]
    assert subuid.status == "broken"
    assert "/etc/subgid" in subuid.detail


def test_rootful_podman_skips_the_rootless_checks():
    facts = rc.RuntimeFacts(
        reported=True,
        name="podman",
        version="5.2.0",
        cdi_devices=("nvidia.com/gpu=all",),
    )
    found = by_check(rc.run_checks(rc.PodmanChecks(), "box", facts, podman_run()))
    assert rc.PODMAN_SOCKET not in found and rc.SUBUID not in found


# ── Nothing answered ─────────────────────────────────────────────────────────


def test_no_daemon_names_the_podman_socket_that_exists():
    """The desktop case: the default socket is rootful Podman's."""
    run = answers(
        {
            rc.SOCKETS_COMMAND: out(
                "/var/run/docker.sock\n/run/user/1000/podman/podman.sock\n"
            )
        }
    )
    found = by_check(
        rc.run_checks(
            rc.UnknownRuntimeChecks(), "desk", rc.RuntimeFacts(reported=True), run
        )
    )
    assert found[rc.NO_RUNTIME].status == "broken"
    assert "/run/user/1000/podman/podman.sock" in found[rc.NO_RUNTIME].detail
    assert "podman.socket" in found[rc.NO_RUNTIME].remedy


# ── Through the backend ──────────────────────────────────────────────────────


def test_the_backend_reads_the_facts_it_is_handed():
    facts = pb.NodeFacts(
        container_runtime="podman",
        runtime_version="4.9.3",
        rootless=True,
        cdi_devices=["nvidia.com/gpu=all"],
    )
    report = DockerBackend().diagnose("n1", "desk", podman_run(), facts)
    assert report.runtime == "podman"


def test_the_preflight_on_a_podman_node_does_not_ask_docker(monkeypatch):
    """What blocked every deploy on the desktop: ``docker info`` via the shim."""
    from tests.test_tools_preflight import CONTROL, Probe, check_of, run

    monkeypatch.setattr(docker_backend, "_reported_facts", lambda _node: PODMAN)
    shim = ProbeResult(
        reachable=True,
        returncode=125,
        stdout='Error: template: info:1:2: executing "info" at <.ServerVersion>',
    )
    overrides: dict[str, Any] = {
        "docker info": shim,
        "podman info": out("/home/ark/.local/share/containers/storage\n"),
        "systemctl --user is-enabled podman.socket": out("enabled\n"),
        "u=$(id -un)": out("/etc/subuid yes\n/etc/subgid yes\n"),
    }
    report = run(probes={CONTROL.id: Probe(CONTROL.address, overrides=overrides)})
    daemon = check_of(report, preflight.CHECK_DOCKER)
    assert daemon["status"] == STATUS_PASS
    assert daemon["title"] == "Podman"
    assert check_of(report, preflight.CHECK_TOOLKIT)["status"] == STATUS_PASS
    assert check_of(report, "podman_socket")["status"] == STATUS_PASS


def test_the_preflight_warns_about_a_socket_that_will_not_survive_a_reboot(
    monkeypatch,
):
    from tests.test_tools_preflight import CONTROL, Probe, check_of, run

    monkeypatch.setattr(docker_backend, "_reported_facts", lambda _node: PODMAN)
    overrides = {
        "podman info": out("/srv/containers\n"),
        "systemctl --user is-enabled podman.socket": out("disabled\n"),
        "u=$(id -un)": out("/etc/subuid no\n/etc/subgid no\n"),
    }
    report = run(probes={CONTROL.id: Probe(CONTROL.address, overrides=overrides)})
    assert check_of(report, "podman_socket")["status"] == STATUS_WARN
    assert check_of(report, "subuid")["status"] == STATUS_FAIL


# ── Where Podman keeps images, without `podman info` (#211) ─────────────────


class TestPodmanStoreCommand:
    """The store command reads containers-storage's own config files.

    Run for real under ``sh``, against config files in a temporary home, with
    the system config pointed at a file this test owns so the machine running
    it cannot change the answer.
    """

    @staticmethod
    def store(home, *, system=None, xdg_config=None, xdg_data=None) -> str:
        import os
        import subprocess

        env = {"PATH": os.environ["PATH"], "HOME": str(home)}
        if xdg_config:
            env["XDG_CONFIG_HOME"] = str(xdg_config)
        if xdg_data:
            env["XDG_DATA_HOME"] = str(xdg_data)
        command = rc.PODMAN_STORE_COMMAND.replace(
            "/usr/share/containers/storage.conf", str(home / "no-vendor.conf")
        ).replace(
            "/etc/containers/storage.conf", str(system or home / "no-system.conf")
        )
        done = subprocess.run(
            ["sh", "-c", command], env=env, capture_output=True, text=True, timeout=10
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    @pytest.fixture(autouse=True)
    def not_root(self):
        import os

        if os.geteuid() == 0:
            pytest.skip("the rootless rules are measured as a user")

    def test_it_never_asks_podman(self):
        assert "podman" not in rc.PODMAN_STORE_COMMAND.split("containers")[0]
        assert "podman info" not in rc.PODMAN_STORE_COMMAND

    def test_with_no_config_it_is_the_rootless_default(self, tmp_path):
        assert self.store(tmp_path) == f"{tmp_path}/.local/share/containers/storage"

    def test_xdg_data_home_moves_the_default(self, tmp_path):
        data = tmp_path / "data"
        assert self.store(tmp_path, xdg_data=data) == f"{data}/containers/storage"

    def test_the_system_rootless_path_is_expanded_as_podman_does(self, tmp_path):
        import getpass
        import os

        system = tmp_path / "storage.conf"
        system.write_text(
            '[storage]\ndriver = "overlay"\n'
            'graphroot = "/var/lib/containers/storage"\n'
            'rootless_storage_path = "$HOME/pods/$USER/$UID"\n'
        )
        assert self.store(tmp_path, system=system) == (
            f"{tmp_path}/pods/{getpass.getuser()}/{os.geteuid()}"
        )

    def test_the_users_graphroot_wins(self, tmp_path):
        config = tmp_path / "cfg"
        (config / "containers").mkdir(parents=True)
        (config / "containers" / "storage.conf").write_text(
            "[storage]\n  graphroot = '/data/pods'\n"
        )
        system = tmp_path / "storage.conf"
        system.write_text('rootless_storage_path = "/elsewhere"\n')
        assert self.store(tmp_path, system=system, xdg_config=config) == "/data/pods"

    def test_a_commented_key_is_not_read(self, tmp_path):
        system = tmp_path / "storage.conf"
        system.write_text('# rootless_storage_path = "/nope"\n')
        assert self.store(tmp_path, system=system) == (
            f"{tmp_path}/.local/share/containers/storage"
        )
