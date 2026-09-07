"""The doctor: what is wrong with a node, and what can be fixed from here.

Built on the same simulated agent_fleet the installer's tests use, and on the same
probes the installer uses — which is the point. A capability measured
correctly for an install is measured correctly for a diagnosis because there
is one probe and two callers.

The three properties that matter more than coverage:

1. it is safe to run when nothing is wrong — asserted as *zero mutations*, not
   as a docstring;
2. it distinguishes "I fixed it" from "I cannot fix this from here" from "this
   needs a human on that machine", and the third is a real category;
3. the control node goes through the same function with no special case.
"""

from __future__ import annotations

import time

import pytest

from spark_pulse.agent.bootstrap import NodeAccess
from spark_pulse.agent.doctor import (
    FIXABLE,
    NEEDS_DECISION,
    NEEDS_HUMAN,
    diagnose,
    treat,
)
from spark_pulse.agent.local import start_local_agent
from spark_pulse.mock.bootstrap_node import _split_operators
from spark_pulse.mock.docker import MockDockerClient, MockDockerService
from tests.agent_bootstrap_fixtures import (
    PASSWORD,
    USER,
    do_install,
    make_node,
    password_prompt,
)

pytestmark = pytest.mark.asyncio

#: Every verb the doctor is allowed to use. Anything outside this set is a
#: mutation, and a diagnosis that mutates is not a diagnosis.
READ_ONLY = {
    "id",
    "printf",
    "hostname",
    "docker",
    "sudo",
    "loginctl",
    "systemctl",
    "command",
    "python3",
    "cat",
    "stat",
    "date",
    "df",
    "test",
}

MUTATING_SYSTEMCTL = ("start", "stop", "restart", "enable", "disable", "daemon-reload")


def mutations(node, since: int = 0) -> list[str]:
    """Every command since ``since`` that could have changed the node.

    Split with the simulator's own quote-aware splitter rather than on ``;``:
    the reachability probe passes a python one-liner containing a semicolon,
    and a naive split would read half of it as a command nobody recognises.
    """
    found = []
    for entry in node.commands[since:]:
        for _operator, clause in _split_operators(entry["command"]):
            parts = clause.split()
            if not parts:
                continue
            if "=" in parts[0] and not parts[0].startswith("/"):
                parts = parts[1:]  # an environment prefix, not a command
            if not parts:
                continue
            verb = parts[0].rsplit("/", 1)[-1]
            if verb == "sudo":
                parts = [p for p in parts[1:] if not p.startswith("-")]
                verb = parts[0].rsplit("/", 1)[-1] if parts else ""
            if verb == "systemctl":
                rest = [p for p in parts[1:] if not p.startswith("--")]
                if rest and rest[0] in MUTATING_SYSTEMCTL:
                    found.append(clause.strip())
                continue
            if verb and verb not in READ_ONLY:
                found.append(clause.strip())
    return found


async def install(agent_server, agent_fleet, node, agent_bundle, **kwargs):
    return await do_install(agent_server, agent_fleet, node, agent_bundle, **kwargs)


def access() -> NodeAccess:
    return NodeAccess(host="10.0.0.7", username=USER)


async def wait_disconnected(agent_server, node_id: str, timeout: float = 5.0) -> None:
    """Let the hub notice a stream that has gone.

    Stopping the unit cancels the agent's task; the control plane learns of it
    when the stream closes, which is a round trip later. Asserting before that
    would be asserting about our own timing rather than about the hub.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not agent_server.hub.is_connected(node_id):
            return
        await asyncio.sleep(0.05)


# ── Healthy ─────────────────────────────────────────────────────────────────


async def test_a_healthy_node_reports_healthy_and_changes_nothing(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    mark = len(node.commands)

    verdict = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert verdict.healthy, [f for f in verdict.problems]
    assert verdict.channels == ["agent", "ssh"]
    assert mutations(node, mark) == []
    assert verdict.repairs == []


async def test_diagnosing_twice_is_the_same_answer_and_still_no_mutation(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    first = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    mark = len(node.commands)
    second = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert [f.check for f in first.findings] == [f.check for f in second.findings]
    assert [f.status for f in first.findings] == [f.status for f in second.findings]
    assert mutations(node, mark) == []


async def test_treating_a_healthy_node_repairs_nothing(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    mark = len(node.commands)
    treated = await treat(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert treated.healthy
    assert treated.repairs == []
    assert mutations(node, mark) == []


# ── Faults ──────────────────────────────────────────────────────────────────


async def test_a_stopped_agent_is_found_and_restarted(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)

    session = await agent_fleet.connect(node.host, USER, password=PASSWORD)
    await session.run(
        "XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop "
        "spark-pulse-agent.service"
    )
    await session.close()
    await wait_disconnected(agent_server, report.node_id)

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert not found.healthy
    unit = found.get("unit")
    assert unit.status == "broken"
    assert unit.verdict == FIXABLE
    assert found.get("agent-connection").status == "broken"

    fixed = await treat(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    restarted = [r for r in fixed.repairs if r.check in ("unit", "agent-connection")]
    assert restarted and all(r.applied for r in restarted)
    # Re-verified, not assumed.
    assert fixed.get("unit").status == "ok"


async def test_lingering_turned_off_is_the_it_works_until_i_log_out_fault(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    node.linger[USER] = False

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    linger = found.get("linger")
    assert linger.status == "broken"
    assert "log out" in linger.detail
    assert linger.verdict == FIXABLE

    fixed = await treat(
        agent_server,
        report.node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
    )
    assert node.linger[USER] is True
    assert fixed.get("linger").status == "ok"


async def test_a_docker_daemon_that_is_down_needs_a_human_on_that_machine(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """Not 'no such container'. Unknown, and not repairable from here."""
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    node.docker_running = False
    mark = len(node.commands)

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    socket = found.get("docker-socket")
    assert socket.status == "broken"
    assert socket.verdict == NEEDS_HUMAN
    assert "not answering" in socket.detail

    treated = await treat(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    declined = [r for r in treated.repairs if r.check == "docker-socket"]
    assert declined and not declined[0].applied
    assert NEEDS_HUMAN in declined[0].detail
    # No doomed attempt: restarting somebody's daemon is not a repair.
    assert not any("docker" in m for m in mutations(node, mark))


async def test_a_user_no_longer_in_docker_is_repairable_with_a_caveat(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    node.docker_socket_users.discard(USER)
    node.users[USER].groups = ("adm", "sudo")

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    socket = found.get("docker-socket")
    assert socket.status == "broken"
    assert socket.verdict == FIXABLE

    treated = await treat(
        agent_server,
        report.node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
    )
    repair = next(r for r in treated.repairs if r.check == "docker-socket")
    assert repair.applied
    assert "next login" in repair.detail
    assert "docker" in node.users[USER].groups


async def test_an_expired_certificate_is_a_decision_not_a_repair(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    entry = agent_server.ledger.get(report.node_id)
    entry.cert_not_after = time.time() - 3600
    mark = len(node.commands)

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    certificate = found.get("certificate")
    assert certificate.status == "broken"
    assert certificate.verdict == NEEDS_DECISION
    assert "re-enrol" in certificate.remedy

    treated = await treat(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    declined = next(r for r in treated.repairs if r.check == "certificate")
    assert not declined.applied
    # Nothing was destroyed to "fix" it.
    assert node.exists(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
    )
    assert mutations(node, mark) == []


async def test_a_certificate_close_to_expiry_is_repaired_by_a_restart(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    entry = agent_server.ledger.get(report.node_id)
    entry.cert_not_after = time.time() + 3600

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert found.get("certificate").status == "warn"
    assert found.get("certificate").verdict == FIXABLE

    treated = await treat(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    repair = next(r for r in treated.repairs if r.check == "certificate")
    assert repair.applied
    assert "restart" in repair.action


async def test_clock_skew_is_named_and_not_corrected(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    node.clock_skew = 3600
    mark = len(node.commands)

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    clock = found.get("clock")
    assert clock.status == "broken"
    assert clock.verdict == NEEDS_HUMAN
    assert "backdated five minutes" in clock.detail

    await treat(agent_server, report.node_id, access=access(), connector=agent_fleet)
    assert mutations(node, mark) == []


async def test_an_agent_behind_the_control_plane_is_reported_as_an_install(
    agent_server, agent_fleet, agent_bundle, tmp_path, monkeypatch
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    monkeypatch.setattr("spark_pulse.agent.doctor.__version__", "99.0.0")

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    version = found.get("agent-version")
    assert version.status == "warn"
    assert "99.0.0" in version.detail
    treated = await treat(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    repair = next(r for r in treated.repairs if r.check == "agent-version")
    assert not repair.applied
    assert "keeps the identity" in repair.detail


async def test_a_rotated_ca_is_a_decision_because_the_pin_exists_to_refuse_it(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    identity_path = node.path(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
    )
    import json

    data = json.loads(identity_path.read_text())
    data["trust_bundle_pin"] = "a-pin-from-a-CA-that-is-gone"
    identity_path.write_text(json.dumps(data))

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    bundle_finding = found.get("trust-bundle")
    assert bundle_finding.status == "broken"
    assert bundle_finding.verdict == NEEDS_DECISION


async def test_a_truncated_ca_file_with_a_matching_pin_is_re_pushed(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    ca = node.path(f"/home/{USER}/.local/share/spark-pulse/agent-identity/ca.pem", USER)
    ca.write_text("-----BEGIN CERTIFICATE-----\ntruncated\n")

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert found.get("trust-bundle").status == "ok"
    assert found.get("trust-bundle-file").status == "warn"

    treated = await treat(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert ca.read_bytes() == agent_server.trust_bundle_pem
    assert treated.get("trust-bundle-file").status == "ok"


async def test_a_loose_private_key_mode_is_tightened(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    key = node.path(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/node.key", USER
    )
    key.chmod(0o644)

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert found.get("identity-permissions").status == "warn"

    treated = await treat(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert key.stat().st_mode & 0o777 == 0o600
    assert treated.get("identity-permissions").status == "ok"


async def test_a_node_that_cannot_reach_the_control_plane_says_so(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    node.can_reach_control_plane = False

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    reach = found.get("reachability")
    assert reach.status == "broken"
    assert reach.verdict == NEEDS_HUMAN


async def test_a_full_disk_is_a_human_problem(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    node.free_bytes = 1024**3

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    disk = found.get("disk")
    assert disk.status == "warn"
    assert disk.verdict == NEEDS_HUMAN
    assert "does not delete anybody's data" in disk.remedy


async def test_a_partial_identity_directory_is_refused_not_repaired(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    identity_path = node.path(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
    )
    identity_path.write_text("{not json at all")

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    identity = found.get("identity")
    assert identity.status == "broken"
    assert identity.verdict == NEEDS_DECISION
    assert "half an identity" in identity.detail


# ── Channels ────────────────────────────────────────────────────────────────


async def test_the_doctor_works_over_ssh_when_the_agent_is_unreachable(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """The case the doctor exists for: the agent channel is gone."""
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    node.kill_agent()
    await wait_disconnected(agent_server, report.node_id)

    found = await diagnose(
        agent_server, report.node_id, access=access(), connector=agent_fleet
    )
    assert "ssh" in found.channels
    assert "agent" not in found.channels
    # SSH still answered everything host-level.
    assert found.get("linger").status == "ok"
    assert found.get("identity").status == "ok"
    assert found.get("docker-socket").status == "ok"
    assert found.get("agent-connection").status == "broken"


async def test_without_ssh_only_the_control_plane_and_agent_checks_run(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await install(agent_server, agent_fleet, node, agent_bundle)
    found = await diagnose(agent_server, report.node_id)
    assert found.channels == ["agent"]
    assert found.get("agent-connection").status == "ok"
    assert found.get("linger").status == "unknown"
    assert found.healthy  # unknown is not a fault


async def test_the_control_node_is_diagnosed_by_the_same_function(
    agent_server, tmp_path, a_runnable_agent_binary
):
    """No special case. The control node's own agent, through ``diagnose``."""
    local = await start_local_agent(
        agent_server,
        directory=tmp_path / "local-agent",
        docker_service=MockDockerService(MockDockerClient()),
        host="127.0.0.1",
        wait=10.0,
    )
    try:
        found = await diagnose(agent_server, local.node_id)
        assert found.channels == ["agent"]
        assert found.get("agent-connection").status == "ok"
        assert found.get("membership").status == "ok"
        assert found.healthy
    finally:
        await local.stop()


async def test_a_node_the_ledger_has_never_heard_of_is_a_decision(agent_server):
    found = await diagnose(agent_server, "00000000-0000-4000-8000-000000000000")
    membership = found.get("membership")
    assert membership.status == "broken"
    assert membership.verdict == NEEDS_DECISION
