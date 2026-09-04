"""Pre-flight checks: each one passing, each one failing, and the verdict.

Every failure assertion here checks three things and not one: that the status
is what it should be, that the *node* is named, and that a *remedy* is given.
A pre-flight that says "docker missing" without saying where or what to do is
the mystery it exists to remove.

No real host is touched. The host probe is the simulated one from
``spark_pulse.mock.preflight`` with per-command overrides, the container
service is a fake bound to a node, and the model verifier is a fake standing in
for the hub-cache walk — which is what lets a two-node failure be exercised on
a machine that is not a Spark and has no peer.
"""

from __future__ import annotations

from typing import Any

import pytest

from spark_pulse.mock.preflight import SimulatedHostProbe
from spark_pulse.tools import preflight
from spark_pulse.tools.preflight import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
    VERDICT_BLOCKED,
    VERDICT_READY,
    VERDICT_SLOW,
    Check,
    NodeTarget,
    ProbeResult,
)

DIGEST = "sha256:" + "a1" * 32
OTHER_DIGEST = "sha256:" + "b2" * 32
IMAGE = f"ghcr.io/lab/spark-pulse-engine/vllm@{DIGEST}"
MODEL = "org/model-8b"
MODEL_BYTES = 16_000_000_000
IMAGE_BYTES = 26_843_545_600

CONTROL = NodeTarget(
    id="control-1",
    label="spark-01",
    address="192.168.1.100",
    is_control_plane=True,
    ethernet_interface="eth0",
    infiniband_interfaces=("ib0",),
    ranks=(0,),
)
PEER = NodeTarget(
    id="peer-1",
    label="spark-02",
    address="10.0.0.11",
    ssh_user="spark",
    ethernet_interface="eth0",
    infiniband_interfaces=("ib0",),
    ranks=(1,),
)


# ── Fakes ────────────────────────────────────────────────────────────────────


class Probe(SimulatedHostProbe):
    """The simulated probe with a per-command-prefix override."""

    def __init__(
        self,
        address: str,
        unreachable: bool = False,
        overrides: dict[str, ProbeResult] | None = None,
    ):
        super().__init__(address, unreachable=unreachable)
        self.overrides = overrides or {}

    def _answer(self, command: str) -> ProbeResult:
        for prefix, result in self.overrides.items():
            if command.startswith(prefix):
                return result
        return super()._answer(command)


class FakeService:
    """A node-bound container service that only has to answer image_info."""

    def __init__(self, info: dict[str, Any] | None, error: str = ""):
        self.info = info
        self.error = error
        self.asked: list[str] = []

    def image_info(self, ref: str) -> dict[str, Any] | None:
        self.asked.append(ref)
        if self.error:
            raise RuntimeError(self.error)
        return self.info


def image_present(digest: str = DIGEST, image_id: str = "sha256:deadbeef"):
    return {
        "id": image_id,
        "size_bytes": IMAGE_BYTES,
        "repo_digests": [f"ghcr.io/lab/spark-pulse-engine/vllm@{digest}"],
        "repo_tags": [],
    }


def model_presence_factory(
    local_state: str = "verified",
    peer_state: str = "verified",
    peer_error: str | None = None,
    missing: list[str] | None = None,
):
    """A stand-in for ``tools.models.presence`` in its real three-state shape."""

    def _row(node: str, state: str) -> dict[str, Any]:
        present = MODEL_BYTES if state == "verified" else 0
        if state == "partial":
            present = int(MODEL_BYTES * 0.9)
        return {
            "node": node,
            "state": state,
            "present": state == "verified",
            "reason": "1 file(s) missing" if state == "partial" else "",
            "bytes_expected": MODEL_BYTES if state != "absent" else MODEL_BYTES,
            "bytes_present": present,
            "files_present": 42 if state == "verified" else 39,
            "files_expected": 42,
            "missing": list(missing or []),
            "missing_count": len(missing or []),
            "error": peer_error,
        }

    def presence(model: str, nodes: list[str], **_: Any) -> dict[str, Any]:
        local = _row("local", local_state)
        return {
            "model": model,
            "revision": "abc",
            "local": local_state == "verified",
            "local_state": local_state,
            "local_report": local,
            "nodes": [_row(node, peer_state) for node in nodes],
        }

    return presence


def make_plan(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "recipe_id": "bundled/demo",
        "engine": "vllm",
        "variant": "default",
        "image_ref": IMAGE,
        "image_size_bytes": IMAGE_BYTES,
        "model": MODEL,
        "port": 9000,
        "rendezvous_port": 29501,
        "nodes": [],
        "node_count": 1,
        "ranks": [{"node_rank": 0, "env": {}}],
    }
    plan.update(overrides)
    return plan


def run(
    targets: list[NodeTarget] | None = None,
    plan: dict[str, Any] | None = None,
    probes: dict[str, Probe] | None = None,
    services: dict[str, FakeService] | None = None,
    presence: Any = None,
) -> dict[str, Any]:
    """The real pre-flight over fakes, with healthy defaults everywhere."""
    targets = targets if targets is not None else [CONTROL]
    plan = plan if plan is not None else make_plan()
    probes = probes if probes is not None else {t.id: Probe(t.address) for t in targets}
    services = (
        services
        if services is not None
        else {t.id: FakeService(image_present()) for t in targets}
    )
    return preflight.run(
        plan=plan,
        targets=targets,
        probe_factory=lambda target: probes[target.id],
        services=lambda node: services[node.id],
        model_presence=presence or model_presence_factory(),
    )


def check_of(report: dict[str, Any], check_id: str, node: str = "") -> dict[str, Any]:
    """The first check with ``check_id`` (optionally on ``node``)."""
    for check in report["checks"]:
        if check["id"] == check_id and (not node or check["node"] == node):
            return check
    raise AssertionError(
        f"no {check_id} check for {node or 'any node'} in "
        f"{[(c['id'], c['node']) for c in report['checks']]}"
    )


def assert_named_failure(check: dict[str, Any], node: str, *phrases: str) -> None:
    """A failure has to name the node and hand over a remedy."""
    assert check["status"] == STATUS_FAIL, check
    assert check["node"] == node
    assert node in check["observed"], check["observed"]
    assert check["remedy"].strip(), "a failing check must say what to do"
    for phrase in phrases:
        assert phrase in (check["observed"] + " " + check["remedy"]).lower()


# ── The happy path ───────────────────────────────────────────────────────────


def test_a_healthy_solo_node_passes_every_check():
    report = run()
    statuses = {c["status"] for c in report["checks"]}
    assert statuses == {STATUS_PASS}, [
        c for c in report["checks"] if c["status"] != STATUS_PASS
    ]
    assert report["verdict"] == VERDICT_READY
    assert report["summary"] == "ready: every check passed"
    assert report["can_proceed"] is True
    assert report["estimated_transfer_bytes"] == 0


def test_every_check_runs_on_every_node():
    report = run(
        targets=[CONTROL, PEER],
        plan=make_plan(nodes=[CONTROL.address, PEER.address], node_count=2),
    )
    for check_id in (
        preflight.CHECK_REACHABILITY,
        preflight.CHECK_DOCKER,
        preflight.CHECK_TOOLKIT,
        preflight.CHECK_GPU,
        preflight.CHECK_IMAGE,
        preflight.CHECK_MODEL,
        preflight.CHECK_PORTS,
        preflight.CHECK_INTERFACES,
        preflight.CHECK_DISK,
    ):
        assert check_of(report, check_id, "spark-01")
        assert check_of(report, check_id, "spark-02")


def test_a_peer_is_actually_asked_rather_than_the_control_node():
    probes = {CONTROL.id: Probe(CONTROL.address), PEER.id: Probe(PEER.address)}
    run(
        targets=[CONTROL, PEER],
        plan=make_plan(nodes=[CONTROL.address, PEER.address], node_count=2),
        probes=probes,
    )
    assert probes[PEER.id].commands, "the peer's probe was never used"
    assert any("nvidia-smi" in c for c in probes[PEER.id].commands)


# ── Reachability ─────────────────────────────────────────────────────────────


def test_an_unreachable_node_fails_and_names_the_node_and_a_remedy():
    report = run(
        targets=[CONTROL, PEER],
        plan=make_plan(nodes=[CONTROL.address, PEER.address], node_count=2),
        probes={
            CONTROL.id: Probe(CONTROL.address),
            PEER.id: Probe(PEER.address, unreachable=True),
        },
    )
    check = check_of(report, preflight.CHECK_REACHABILITY, "spark-02")
    assert_named_failure(check, "spark-02", "ssh")
    assert report["verdict"] == VERDICT_BLOCKED
    assert "spark-02" in report["summary"]


def test_an_unreachable_node_reports_one_failure_not_nine():
    report = run(
        targets=[PEER],
        plan=make_plan(nodes=[PEER.address], node_count=1),
        probes={PEER.id: Probe(PEER.address, unreachable=True)},
    )
    assert [c["id"] for c in report["checks"]] == [preflight.CHECK_REACHABILITY]


def test_a_failed_command_is_told_apart_from_an_unreachable_node():
    """Phase A made this structural; the report has to keep it that way."""
    broken = ProbeResult(reachable=True, returncode=1, stderr="stdin: is not a tty")
    report = run(
        probes={CONTROL.id: Probe(CONTROL.address, overrides={"echo": broken})},
    )
    check = check_of(report, preflight.CHECK_REACHABILITY)
    assert_named_failure(check, "spark-01", "shell")
    assert "answered" in check["observed"]
    assert check["detail"]["reachable"] is True


# ── Docker and the container toolkit ─────────────────────────────────────────


def _stdout(text: str) -> ProbeResult:
    return ProbeResult(reachable=True, returncode=0, stdout=text)


#: The toolkit probe finding nothing on PATH and no CDI spec.
NO_HOOK = {"command -v nvidia-container-runtime-hook": _stdout("")}

#: The other way round: no hook, but a runtime registered the old way.
RUNTIME_NO_HOOK = {
    **NO_HOOK,
    "docker info": _stdout("27.5.1|/var/lib/docker|nvidia runc "),
}

#: …and docker info reporting no nvidia runtime either.
NO_HOOK_NO_RUNTIME = {
    **NO_HOOK,
    "docker info": _stdout("27.5.1|/var/lib/docker|runc "),
}


def test_docker_present_and_responding_passes():
    check = check_of(run(), preflight.CHECK_DOCKER)
    assert check["status"] == STATUS_PASS
    assert "27.5.1" in check["observed"]


def test_docker_missing_fails_with_an_install_remedy():
    missing = ProbeResult(
        reachable=True, returncode=127, stderr="sh: 1: docker: not found"
    )
    report = run(
        probes={CONTROL.id: Probe(CONTROL.address, overrides={"docker info": missing})}
    )
    assert_named_failure(
        check_of(report, preflight.CHECK_DOCKER), "spark-01", "install docker"
    )
    assert report["verdict"] == VERDICT_BLOCKED


def test_a_docker_daemon_that_does_not_answer_fails_differently():
    down = ProbeResult(
        reachable=True,
        returncode=1,
        stdout="Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    )
    check = check_of(
        run(
            probes={CONTROL.id: Probe(CONTROL.address, overrides={"docker info": down})}
        ),
        preflight.CHECK_DOCKER,
    )
    assert_named_failure(check, "spark-01", "systemctl start docker")
    assert "not installed" not in check["observed"]


def test_the_toolkit_hook_passes_without_a_registered_nvidia_runtime():
    # This is the real DGX Spark's shape and the reason the check was rewritten:
    # docker info lists only runc, the toolkit is installed as an OCI hook, and
    # --gpus all works. Demanding a runtime named "nvidia" blocked every deploy
    # on a correctly configured machine.
    check = check_of(run(), preflight.CHECK_TOOLKIT)
    assert check["status"] == STATUS_PASS
    assert "nvidia" not in " ".join(check["detail"]["runtimes"])
    assert check["detail"]["hook"] is True
    assert "--gpus all" in check["observed"]


def test_a_registered_nvidia_runtime_passes_when_the_hook_is_not_on_path():
    check = check_of(
        run(probes={CONTROL.id: Probe(CONTROL.address, overrides=RUNTIME_NO_HOOK)}),
        preflight.CHECK_TOOLKIT,
    )
    assert check["status"] == STATUS_PASS
    assert "nvidia" in check["detail"]["runtimes"]


def test_cdi_device_specs_alone_pass():
    check = check_of(
        run(
            probes={
                CONTROL.id: Probe(
                    CONTROL.address,
                    overrides={
                        **NO_HOOK_NO_RUNTIME,
                        "command -v nvidia-container-runtime-hook": _stdout(
                            "cdi /etc/cdi/nvidia.yaml"
                        ),
                    },
                )
            }
        ),
        preflight.CHECK_TOOLKIT,
    )
    assert check["status"] == STATUS_PASS
    assert check["detail"]["cdi_specs"] == ["/etc/cdi/nvidia.yaml"]


def test_no_gpu_path_at_all_fails_because_the_deploy_asks_for_every_gpu():
    check = check_of(
        run(probes={CONTROL.id: Probe(CONTROL.address, overrides=NO_HOOK_NO_RUNTIME)}),
        preflight.CHECK_TOOLKIT,
    )
    assert_named_failure(check, "spark-01", "nvidia-ctk runtime configure")


def test_parse_toolkit_reads_the_probe_evidence():
    assert preflight.parse_toolkit("hook\nctk\ncdi /etc/cdi/nvidia.yaml") == {
        "hook": True,
        "ctk": True,
        "cdi": ["/etc/cdi/nvidia.yaml"],
    }
    assert preflight.parse_toolkit("") == {"hook": False, "ctk": False, "cdi": []}


# ── GPU, and the unified-memory honesty ──────────────────────────────────────


def test_a_gpu_with_reported_memory_passes_with_the_nvidia_smi_figure():
    reported = ProbeResult(
        reachable=True, returncode=0, stdout="0, NVIDIA H100 80GB HBM3, 81559, 79000"
    )
    check = check_of(
        run(
            probes={
                CONTROL.id: Probe(CONTROL.address, overrides={"nvidia-smi": reported})
            }
        ),
        preflight.CHECK_GPU,
    )
    assert check["status"] == STATUS_PASS
    assert check["detail"]["memory_source"] == "nvidia-smi"


def test_no_gpu_visible_fails_and_names_the_node():
    none = ProbeResult(
        reachable=True, returncode=127, stderr="nvidia-smi: command not found"
    )
    check = check_of(
        run(
            probes={CONTROL.id: Probe(CONTROL.address, overrides={"nvidia-smi": none})}
        ),
        preflight.CHECK_GPU,
    )
    assert_named_failure(check, "spark-01", "driver")


def test_unreported_gpu_memory_does_not_fail_the_node():
    """On this hardware nvidia-smi reports no GPU memory at all. Ever.

    Believing the ``[N/A]`` as zero would fail a perfectly healthy Spark, so
    the free figure comes from ``/proc/meminfo`` and the node passes.
    """
    check = check_of(run(), preflight.CHECK_GPU)
    assert check["status"] == STATUS_PASS
    assert check["detail"]["memory_source"] == "meminfo"
    assert check["detail"]["gpus"][0]["memory_free_mib"] is None
    assert "unified" in check["observed"]
    assert "/proc/meminfo" in check["observed"]


def test_gpu_memory_unavailable_everywhere_warns_rather_than_fails():
    no_meminfo = ProbeResult(reachable=True, returncode=1, stderr="No such file")
    check = check_of(
        run(
            probes={
                CONTROL.id: Probe(
                    CONTROL.address, overrides={"cat /proc/meminfo": no_meminfo}
                )
            }
        ),
        preflight.CHECK_GPU,
    )
    assert check["status"] == STATUS_WARN
    assert check["detail"]["free_bytes"] is None
    assert "unavailable" in check["observed"]
    assert check["remedy"]


def test_gpu_memory_unavailable_is_an_advisory_not_a_delay():
    no_meminfo = ProbeResult(reachable=True, returncode=1, stderr="No such file")
    report = run(
        probes={
            CONTROL.id: Probe(
                CONTROL.address, overrides={"cat /proc/meminfo": no_meminfo}
            )
        }
    )
    assert report["verdict"] == VERDICT_READY
    assert [c["id"] for c in report["advisories"]] == [preflight.CHECK_GPU]
    assert report["delaying"] == []


# ── Image ────────────────────────────────────────────────────────────────────


def test_a_matching_digest_passes():
    check = check_of(run(), preflight.CHECK_IMAGE)
    assert check["status"] == STATUS_PASS
    assert check["detail"]["pull_required"] is False


def test_an_absent_image_is_a_warning_not_a_failure():
    """A pull is a wait, not a defect. Failing here trains operators to ignore
    the pre-flight, which is the only way it can actually do harm."""
    report = run(services={CONTROL.id: FakeService(None)})
    check = check_of(report, preflight.CHECK_IMAGE)
    assert check["status"] == STATUS_WARN
    assert check["node"] == "spark-01"
    assert check["detail"]["pull_required"] is True
    assert check["delay_bytes"] == IMAGE_BYTES
    assert "25.0 GB" in check["observed"]
    assert "spark-01" in check["observed"]
    assert "/api/images/sync" in check["remedy"]
    assert report["can_proceed"] is True


def test_digest_drift_warns_and_says_a_pull_is_needed():
    report = run(
        services={
            CONTROL.id: FakeService(image_present(digest=OTHER_DIGEST, image_id="x"))
        }
    )
    check = check_of(report, preflight.CHECK_IMAGE)
    assert check["status"] == STATUS_WARN
    assert OTHER_DIGEST in check["observed"]
    assert DIGEST in check["observed"]
    assert check["detail"]["pull_required"] is True


def test_an_image_we_cannot_ask_about_assumes_a_pull_rather_than_claiming_one():
    report = run(services={CONTROL.id: FakeService(None, error="daemon is gone")})
    check = check_of(report, preflight.CHECK_IMAGE)
    assert check["status"] == STATUS_WARN
    assert "daemon is gone" in check["observed"]
    assert "spark-01" in check["observed"]
    assert check["remedy"]


def test_an_absent_image_makes_the_deployment_slow_not_blocked():
    report = run(services={CONTROL.id: FakeService(None)})
    assert report["verdict"] == VERDICT_SLOW
    assert report["estimated_transfer_bytes"] == IMAGE_BYTES
    assert "25.0 GB" in report["summary"]


# ── Model ────────────────────────────────────────────────────────────────────


def test_a_verified_model_passes():
    check = check_of(run(), preflight.CHECK_MODEL)
    assert check["status"] == STATUS_PASS
    assert check["detail"]["state"] == "verified"


def test_a_partial_model_warns_and_names_what_is_missing():
    report = run(
        targets=[CONTROL, PEER],
        plan=make_plan(nodes=[CONTROL.address, PEER.address], node_count=2),
        services={
            CONTROL.id: FakeService(image_present()),
            PEER.id: FakeService(image_present()),
        },
        presence=model_presence_factory(
            peer_state="partial",
            missing=["model-00040-of-00042.safetensors"],
        ),
    )
    check = check_of(report, preflight.CHECK_MODEL, "spark-02")
    assert check["status"] == STATUS_WARN
    assert "spark-02" in check["observed"]
    assert "model-00040-of-00042.safetensors" in check["observed"]
    assert "replicate" in check["remedy"]
    assert check["delay_bytes"] > 0


def test_an_absent_model_warns_with_the_bytes_that_must_move():
    report = run(presence=model_presence_factory(local_state="absent"))
    check = check_of(report, preflight.CHECK_MODEL)
    assert check["status"] == STATUS_WARN
    assert check["delay_bytes"] == MODEL_BYTES
    assert report["verdict"] == VERDICT_SLOW


def test_a_model_we_cannot_verify_fails_and_names_the_node():
    def presence(model: str, nodes: list[str], **_: Any) -> dict[str, Any]:
        raise RuntimeError("python3 not found on the node")

    report = run(presence=presence)
    check = check_of(report, preflight.CHECK_MODEL)
    assert_named_failure(check, "spark-01", "python3")


def test_a_recipe_with_no_model_of_its_own_passes_the_model_check():
    check = check_of(run(plan=make_plan(model="")), preflight.CHECK_MODEL)
    assert check["status"] == STATUS_PASS


# ── Ports ────────────────────────────────────────────────────────────────────


def test_a_free_api_port_passes_on_every_node():
    report = run(
        targets=[CONTROL, PEER],
        plan=make_plan(nodes=[CONTROL.address, PEER.address], node_count=2),
        services={
            CONTROL.id: FakeService(image_present()),
            PEER.id: FakeService(image_present()),
        },
    )
    for node in ("spark-01", "spark-02"):
        check = check_of(report, preflight.CHECK_PORTS, node)
        assert check["status"] == STATUS_PASS


def busy(*ports: int) -> ProbeResult:
    body = "\n".join(f"LISTEN 0 4096 0.0.0.0:{p} 0.0.0.0:*" for p in ports)
    return ProbeResult(reachable=True, returncode=0, stdout=body)


def test_a_busy_api_port_fails_on_the_peer_not_only_on_the_control_plane():
    report = run(
        targets=[CONTROL, PEER],
        plan=make_plan(nodes=[CONTROL.address, PEER.address], node_count=2),
        probes={
            CONTROL.id: Probe(CONTROL.address),
            PEER.id: Probe(PEER.address, overrides={"ss -H": busy(9000)}),
        },
        services={
            CONTROL.id: FakeService(image_present()),
            PEER.id: FakeService(image_present()),
        },
    )
    check = check_of(report, preflight.CHECK_PORTS, "spark-02")
    assert_named_failure(check, "spark-02", "9000")
    assert check_of(report, preflight.CHECK_PORTS, "spark-01")["status"] == STATUS_PASS
    assert report["verdict"] == VERDICT_BLOCKED


def test_a_busy_rendezvous_port_fails_above_one_node():
    report = run(
        targets=[CONTROL, PEER],
        plan=make_plan(nodes=[CONTROL.address, PEER.address], node_count=2),
        probes={
            CONTROL.id: Probe(CONTROL.address, overrides={"ss -H": busy(29501)}),
            PEER.id: Probe(PEER.address),
        },
        services={
            CONTROL.id: FakeService(image_present()),
            PEER.id: FakeService(image_present()),
        },
    )
    check = next(
        c
        for c in report["checks"]
        if c["id"] == preflight.CHECK_PORTS
        and c["detail"].get("role") == "rendezvous port"
        and c["node"] == "spark-01"
    )
    assert_named_failure(check, "spark-01", "29501")


def test_a_busy_rendezvous_port_only_warns_at_one_node():
    """Below two nodes the engine derives a file-based store and never binds it."""
    report = run(
        probes={CONTROL.id: Probe(CONTROL.address, overrides={"ss -H": busy(29501)})}
    )
    check = next(
        c
        for c in report["checks"]
        if c["id"] == preflight.CHECK_PORTS
        and c["detail"].get("role") == "rendezvous port"
    )
    assert check["status"] == STATUS_WARN
    assert "spark-01" in check["observed"]
    assert check["remedy"]
    assert report["verdict"] == VERDICT_READY


def test_ports_we_cannot_list_warn_rather_than_claim_they_are_free():
    nothing = ProbeResult(reachable=True, returncode=1, stderr="ss: not found")
    check = check_of(
        run(probes={CONTROL.id: Probe(CONTROL.address, overrides={"ss -H": nothing})}),
        preflight.CHECK_PORTS,
    )
    assert check["status"] == STATUS_WARN
    assert "spark-01" in check["observed"]
    assert check["remedy"]


# ── Interfaces ───────────────────────────────────────────────────────────────


def two_node_plan(**over: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "nodes": [CONTROL.address, PEER.address],
        "node_count": 2,
        "ranks": [
            {"node_rank": 0, "env": {"NCCL_SOCKET_IFNAME": "enp1s0f0np0"}},
            {"node_rank": 1, "env": {"NCCL_SOCKET_IFNAME": "enp1s0f0np0"}},
        ],
    }
    defaults.update(over)
    return make_plan(**defaults)


def two_node_services() -> dict[str, FakeService]:
    return {
        CONTROL.id: FakeService(image_present()),
        PEER.id: FakeService(image_present()),
    }


def test_pinned_interfaces_that_exist_pass():
    report = run(
        targets=[CONTROL, PEER], plan=two_node_plan(), services=two_node_services()
    )
    for node in ("spark-01", "spark-02"):
        assert check_of(report, preflight.CHECK_INTERFACES, node)["status"] == (
            STATUS_PASS
        )


def test_a_missing_pinned_interface_fails_and_explains_find_or_fail():
    report = run(
        targets=[CONTROL, PEER],
        plan=two_node_plan(
            ranks=[
                {"node_rank": 0, "env": {"NCCL_SOCKET_IFNAME": "enp1s0f0np0"}},
                {"node_rank": 1, "env": {"NCCL_SOCKET_IFNAME": "enp9s0f9np9"}},
            ]
        ),
        services=two_node_services(),
    )
    check = check_of(report, preflight.CHECK_INTERFACES, "spark-02")
    assert_named_failure(check, "spark-02", "enp9s0f9np9", "find-or-fail")
    assert check_of(report, preflight.CHECK_INTERFACES, "spark-01")["status"] == (
        STATUS_PASS
    )
    assert report["verdict"] == VERDICT_BLOCKED


def test_the_registry_supplies_the_names_when_the_plan_pins_none_yet():
    report = run(
        targets=[CONTROL, PEER],
        plan=two_node_plan(
            ranks=[{"node_rank": 0, "env": {}}, {"node_rank": 1, "env": {}}]
        ),
        services=two_node_services(),
    )
    names = {
        c["detail"].get("interface")
        for c in report["checks"]
        if c["id"] == preflight.CHECK_INTERFACES
    }
    assert names == {"eth0", "ib0"}


def test_interfaces_are_not_checked_at_one_node():
    """The engines gate fabric pinning on the node count; so does this."""
    report = run()
    assert not [c for c in report["checks"] if c["id"] == preflight.CHECK_INTERFACES]


def test_a_plan_pinning_nothing_at_all_warns_about_autoselection():
    bare = NodeTarget(id="bare", label="spark-03", address="10.0.0.12", ranks=(1,))
    report = run(
        targets=[CONTROL, bare],
        plan=two_node_plan(
            nodes=[CONTROL.address, bare.address],
            ranks=[{"node_rank": 0, "env": {}}, {"node_rank": 1, "env": {}}],
        ),
        services={
            CONTROL.id: FakeService(image_present()),
            bare.id: FakeService(image_present()),
        },
    )
    check = check_of(report, preflight.CHECK_INTERFACES, "spark-03")
    assert check["status"] == STATUS_WARN
    assert "spark-03" in check["observed"]
    assert check["remedy"]


# ── Disk ─────────────────────────────────────────────────────────────────────


def df_free(kb: int) -> ProbeResult:
    def _lines(command: str) -> str:
        out = []
        for path in ("/var/lib/docker", "hub"):
            out.append(f"== {path}")
            out.append(f"/dev/nvme0n1p2 {kb * 2} {kb} {kb} 50% /")
        return "\n".join(out)

    return ProbeResult(reachable=True, returncode=0, stdout=_lines(""))


def test_enough_disk_passes_and_says_how_much_is_needed():
    report = run(services={CONTROL.id: FakeService(None)})
    check = check_of(report, preflight.CHECK_DISK)
    assert check["status"] == STATUS_PASS
    assert check["detail"]["needed_bytes"] > IMAGE_BYTES


def test_too_little_disk_fails_and_names_the_node_and_a_remedy():
    tight = ProbeResult(
        reachable=True,
        returncode=0,
        stdout="== /var/lib/docker\n/dev/nvme0n1p2 100000000 99000000 1000000 99% /",
    )
    report = run(
        services={CONTROL.id: FakeService(None)},
        probes={CONTROL.id: Probe(CONTROL.address, overrides={"for p in": tight})},
    )
    check = check_of(report, preflight.CHECK_DISK)
    assert_named_failure(check, "spark-01", "prune")
    assert report["verdict"] == VERDICT_BLOCKED


def test_disk_we_cannot_measure_warns_when_something_has_to_land():
    blind = ProbeResult(reachable=True, returncode=1, stderr="df: not found")
    check = check_of(
        run(
            services={CONTROL.id: FakeService(None)},
            probes={CONTROL.id: Probe(CONTROL.address, overrides={"for p in": blind})},
        ),
        preflight.CHECK_DISK,
    )
    assert check["status"] == STATUS_WARN
    assert "spark-01" in check["observed"]
    assert check["remedy"]


def test_a_model_of_unknown_size_is_reported_rather_than_guessed():
    def presence(model: str, nodes: list[str], **_: Any) -> dict[str, Any]:
        return {
            "model": model,
            "local_state": "absent",
            "local_report": {"state": "absent", "bytes_expected": 0},
            "nodes": [],
        }

    check = check_of(run(presence=presence), preflight.CHECK_DISK)
    assert check["status"] == STATUS_WARN
    assert "how big" in check["observed"]
    assert check["remedy"]


# ── The verdict ──────────────────────────────────────────────────────────────


def _c(status: str, node: str = "spark-01", delay: int = 0, costs: bool = False):
    return Check(
        id="x",
        title="t",
        node=node,
        node_id=node,
        status=status,
        observed="o",
        remedy="r",
        delay_bytes=delay,
        costs_time=costs or delay > 0,
    )


def test_the_verdict_separates_blocked_from_slow():
    blocked, why = preflight.verdict_for(
        [_c(STATUS_FAIL), _c(STATUS_WARN, delay=IMAGE_BYTES)]
    )
    assert blocked == VERDICT_BLOCKED
    assert "cannot proceed" in why

    slow, why = preflight.verdict_for(
        [_c(STATUS_PASS), _c(STATUS_WARN, delay=IMAGE_BYTES)]
    )
    assert slow == VERDICT_SLOW
    assert "25.0 GB" in why


def test_a_warning_that_costs_nothing_does_not_make_a_deployment_slow():
    verdict, why = preflight.verdict_for([_c(STATUS_PASS), _c(STATUS_WARN)])
    assert verdict == VERDICT_READY
    assert "advisory" in why


def test_a_transfer_of_unknown_size_is_still_slow():
    verdict, why = preflight.verdict_for([_c(STATUS_WARN, costs=True)])
    assert verdict == VERDICT_SLOW
    assert "unreported size" in why


def test_the_verdict_names_the_nodes_that_blocked_it():
    verdict, why = preflight.verdict_for(
        [_c(STATUS_FAIL, node="spark-02"), _c(STATUS_FAIL, node="spark-03")]
    )
    assert verdict == VERDICT_BLOCKED
    assert "spark-02" in why and "spark-03" in why


def test_every_non_passing_check_names_its_node_and_a_remedy():
    """The whole contract, swept across a deliberately unhappy cluster."""
    report = run(
        targets=[CONTROL, PEER],
        plan=two_node_plan(
            ranks=[
                {"node_rank": 0, "env": {"NCCL_SOCKET_IFNAME": "nope0"}},
                {"node_rank": 1, "env": {"NCCL_SOCKET_IFNAME": "enp1s0f0np0"}},
            ]
        ),
        probes={
            CONTROL.id: Probe(CONTROL.address, overrides={"ss -H": busy(9000)}),
            PEER.id: Probe(PEER.address),
        },
        services={CONTROL.id: FakeService(None), PEER.id: FakeService(None)},
        presence=model_presence_factory(local_state="absent", peer_state="partial"),
    )
    bad = [c for c in report["checks"] if c["status"] != STATUS_PASS]
    assert len(bad) >= 6
    for check in bad:
        assert check["node"] in {"spark-01", "spark-02"}, check
        assert check["node"] in check["observed"], check
        assert check["remedy"].strip(), check


# ── Parsing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*", {8000}),
        ("LISTEN 0 4096 [::]:8000 [::]:*", {8000}),
        ("tcp4  0  0  *.8000   *.*   LISTEN", {8000}),
        ("", set()),
    ],
)
def test_listening_ports_are_parsed_from_both_tools(text, expected):
    assert preflight.parse_listening_ports(text) >= expected


def test_meminfo_is_parsed_into_bytes():
    parsed = preflight.parse_meminfo("MemTotal:  1024 kB\nMemAvailable:  512 kB\n")
    assert parsed["MemTotal"] == 1024 * 1024
    assert parsed["MemAvailable"] == 512 * 1024


def test_na_gpu_memory_stays_none_rather_than_becoming_zero():
    """Zero would read as "no memory", which is a different and false claim."""
    gpus = preflight.parse_gpu_query("0, NVIDIA GB10, [N/A], [N/A]")
    assert gpus[0]["name"] == "NVIDIA GB10"
    assert gpus[0]["memory_total_mib"] is None
    assert gpus[0]["memory_free_mib"] is None


def test_df_output_is_parsed_per_path():
    parsed = preflight.parse_df(
        "== /var/lib/docker\n/dev/nvme0n1p2 1000 400 600 40% /\n"
    )
    assert parsed["/var/lib/docker"]["available_bytes"] == 600 * 1024
    assert parsed["/var/lib/docker"]["mount"] == "/"


def test_docker_runtimes_are_parsed_from_the_json_blob():
    assert "nvidia" in preflight.parse_runtimes(
        '{"nvidia":{"path":"nvidia-container-runtime"},"runc":{"path":"runc"}}'
    )


def test_the_disk_command_quotes_its_paths():
    assert "'/a b'" in preflight.disk_command(["/a b"])


# ── The simulated twin ───────────────────────────────────────────────────────

RECIPE = "bundled/qwen2.5-0.5b-instruct"

#: One GPU per node, so a two-node plan has to occupy two ranks; a recipe left
#: at tp=1 is refused before the pre-flight has a plan to check.
TWO_WAY = {"tensor_parallel": 2}


def test_the_mock_twin_exports_everything_the_real_module_does():
    """Both ``__init__`` lists carry preflight, so both must satisfy callers."""
    from spark_pulse.mock import preflight as mock_preflight

    missing = set(preflight.__all__) - set(dir(mock_preflight))
    assert not missing, f"the mock is missing {sorted(missing)}"


def test_simulation_runs_the_real_checks_over_a_simulated_host():
    from spark_pulse import tools

    report = tools.preflight.run(RECIPE)
    assert report["verdict"] in preflight.VERDICTS
    assert report["plan"]["engine"]
    gpu = check_of(report, preflight.CHECK_GPU)
    # The simulated Spark reports no GPU memory, as the real one does.
    assert gpu["status"] == STATUS_PASS
    assert gpu["detail"]["memory_source"] == "meminfo"


def test_simulation_reports_a_peer_that_has_to_pull_the_image():
    from spark_pulse import tools

    report = tools.preflight.run(
        RECIPE, nodes=["192.168.1.100", "10.0.0.11"], params=TWO_WAY
    )
    image = check_of(report, preflight.CHECK_IMAGE, "spark-02")
    assert image["status"] == STATUS_WARN
    assert image["detail"]["pull_required"] is True
    assert report["verdict"] == VERDICT_SLOW


def test_simulation_can_make_a_node_unreachable():
    from spark_pulse import tools
    from spark_pulse.mock import preflight as mock_preflight

    mock_preflight.UNREACHABLE.add("10.0.0.11")
    report = tools.preflight.run(
        RECIPE, nodes=["192.168.1.100", "10.0.0.11"], params=TWO_WAY
    )
    assert report["verdict"] == VERDICT_BLOCKED
    assert_named_failure(
        check_of(report, preflight.CHECK_REACHABILITY, "spark-02"), "spark-02"
    )


def test_a_solo_plan_targets_the_control_node_rather_than_nothing():
    """A cluster of size one is a cluster, and its one node is this machine."""
    targets = preflight.targets_for(make_plan())
    assert len(targets) == 1
    assert targets[0].is_control_plane is True
    assert targets[0].label == "spark-01"


def test_targets_carry_the_registry_names_and_ssh_users():
    targets = preflight.targets_for(
        make_plan(nodes=["192.168.1.100", "10.0.0.11"], node_count=2)
    )
    assert [t.label for t in targets] == ["spark-01", "spark-02"]
    assert targets[0].is_control_plane is True
    assert targets[1].ssh_user == "spark"
    assert targets[1].ranks == (1,)


def test_an_address_the_registry_has_never_seen_is_still_a_target():
    targets = preflight.targets_for(make_plan(nodes=["10.9.9.9"], node_count=1))
    assert [t.label for t in targets] == ["10.9.9.9"]
    assert targets[0].is_control_plane is False
