"""The SSH bootstrap installer, against nodes that do not exist.

Everything here is real except the machine. The control plane is a real
:class:`ControlPlaneServer` on ephemeral loopback ports with a real CA; the
the bundle is really built and really unpacked; the enrolment is a real gRPC call
over real TLS; and the agent that appears in the hub is a real
:class:`NodeAgent` holding a real mTLS stream. What is simulated is the node —
its filesystem, its logins, its sudo policy, its Docker socket and its systemd
— because that is the thing there is a matrix of, and the matrix is where the
installer is either right or wrong.

The properties, in the order they cost the most to get wrong:

* a password and a private key appear in no log record and in no command line,
  ever, on any path (:func:`test_no_secret_reaches_a_log_record_or_a_command`);
* the least-privilege install is chosen, and a node that needs no elevation
  gets **zero** sudo calls;
* an existing identity converges or is refused loudly, never ignored;
* a token works once;
* what could not be obtained is reported, not swallowed.
"""

from __future__ import annotations

import logging

import pytest

from spark_pulse.agent import bootstrap
from spark_pulse.agent.bootstrap import (
    ExistingIdentity,
    NodeAccess,
    install_agent,
    remove_node_and_identity,
    uninstall_agent_keep_identity,
)
from spark_pulse.agent.bootstrap_probe import probe_node
from spark_pulse.agent.bootstrap_transport import (
    AuthFailed,
    BootstrapError,
    HostKeyDeclined,
    RootPasswordBootstrap,
    generate_keypair,
)
from spark_pulse.mock.bootstrap_node import SimulatedNode, SimulatedUser, SudoPolicy
from tests.agent_bootstrap_fixtures import (
    HOST,
    PASSWORD,
    SUDO_PASSWORD,
    USER,
    confirm,
    decline,
    do_install,
    make_node,
    password_prompt,
    sudo_elevations,
)

pytestmark = pytest.mark.asyncio


# ── The happy path, end to end ──────────────────────────────────────────────


async def test_install_enrol_connect_on_a_node_that_needs_no_root(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """The measured Spark: docker group, linger on. Zero sudo, and connected."""
    node = make_node(tmp_path)
    report = await do_install(agent_server, agent_fleet, node, agent_bundle)

    assert report.scope == "user"
    assert report.connected is True
    assert report.node_id
    assert agent_server.hub.is_connected(report.node_id)
    assert agent_server.ledger.get(report.node_id) is not None

    # The whole point of preferring a user unit.
    assert sudo_elevations(node) == []
    assert node.sudo_prompts == 0
    assert report.privileged_calls == []
    assert report.concessions == []

    # It really is installed: a unit, a symlinked bundle, an identity.
    assert node.exists(
        f"/home/{USER}/.config/systemd/user/spark-pulse-agent.service", USER
    )
    assert node.exists(f"/home/{USER}/.local/share/spark-pulse/agent/current", USER)
    assert node.exists(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
    )
    unit = node.read(
        f"/home/{USER}/.config/systemd/user/spark-pulse-agent.service", USER
    ).decode()
    assert "RestartPreventExitStatus=2" in unit
    assert "SPARK_PULSE_AGENT_DIR=" in unit


async def test_the_token_file_is_gone_and_the_token_is_dead(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """§3.1 step 8: invalidated on the control plane, overwritten on the host."""
    node = make_node(tmp_path)
    await do_install(agent_server, agent_fleet, node, agent_bundle)

    staging = f"/home/{USER}/.cache/spark-pulse/bootstrap"
    assert not node.exists(f"{staging}/token", USER)

    # Every token this control plane ever minted is spent.
    grants = (
        agent_server.ledger._state.tokens.values()
    )  # noqa: SLF001 - the ledger's own state
    assert grants and all(grant.used_at for grant in grants)


async def test_a_replayed_token_is_refused(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """One node, one token, one use. A second enrolment with it is rejected."""
    from spark_pulse.agent.errors import EnrollmentRejected
    from spark_pulse.agent.node_agent import enroll

    token = agent_server.mint_token("worker-1")
    identity = await enroll(
        agent_server.enrollment_target(),
        token,
        trust_bundle_pem=agent_server.trust_bundle_pem,
        trust_bundle_pin=agent_server.trust_bundle_pin,
        directory=tmp_path / "first",
        requested_name="worker-1",
    )
    assert identity.node_id

    with pytest.raises(Exception) as caught:
        await enroll(
            agent_server.enrollment_target(),
            token,
            trust_bundle_pem=agent_server.trust_bundle_pem,
            trust_bundle_pin=agent_server.trust_bundle_pin,
            directory=tmp_path / "second",
            requested_name="worker-1",
        )
    assert "already used" in str(caught.value) or isinstance(
        caught.value, EnrollmentRejected
    )


# ── The operator's part ─────────────────────────────────────────────────────


async def test_a_declined_host_key_sends_nothing(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    agent_fleet.add(node)
    with pytest.raises(HostKeyDeclined):
        await install_agent(
            agent_server,
            host=node.host,
            username=USER,
            control_host="127.0.0.1",
            connector=agent_fleet,
            bundle=agent_bundle,
            confirm_host_key=decline,
            password_prompt=password_prompt(PASSWORD),
        )
    # Not one command ran, so not one byte of the password could have moved.
    assert node.commands == []
    assert node.uploads == []


async def test_the_fingerprint_is_offered_before_the_password_is_asked_for(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """Order matters: §3.1 requires confirmation *before* the password is sent."""
    order: list[str] = []

    async def confirming(host_key):
        order.append(f"fingerprint:{host_key.fingerprint}")
        return True

    async def asking(_question):
        order.append("password")
        return PASSWORD

    node = make_node(tmp_path)
    await do_install(
        agent_server,
        agent_fleet,
        node,
        agent_bundle,
        confirm_host_key=confirming,
        password_prompt=asking,
    )
    assert order[0].startswith("fingerprint:SHA256:")
    assert order[1] == "password"


async def test_a_wrong_password_fails_before_anything_is_installed(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    agent_fleet.add(node)
    with pytest.raises(AuthFailed):
        await install_agent(
            agent_server,
            host=node.host,
            username=USER,
            control_host="127.0.0.1",
            connector=agent_fleet,
            bundle=agent_bundle,
            confirm_host_key=confirm,
            password_prompt=password_prompt("not-the-password"),
        )
    assert node.commands == []
    assert not node.exists(f"/home/{USER}/.local/share/spark-pulse/agent", USER)


async def test_root_with_a_password_is_named_not_guessed(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """Ubuntu 24.04's PermitRootLogin=prohibit-password, detected up front."""
    node = SimulatedNode(
        HOST,
        tmp_path / "rootnode",
        users={"root": SimulatedUser(name="root", uid=0, password="hunter2")},
    )
    agent_fleet.add(node)
    with pytest.raises(RootPasswordBootstrap) as caught:
        await install_agent(
            agent_server,
            host=HOST,
            username="root",
            control_host="127.0.0.1",
            connector=agent_fleet,
            bundle=agent_bundle,
            confirm_host_key=confirm,
            password_prompt=password_prompt("hunter2"),
        )
    assert "prohibit-password" in str(caught.value)
    # Named before the password was even asked for, let alone sent.
    assert node.commands == []


async def test_only_the_public_half_of_the_key_ever_reaches_the_node(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    await do_install(agent_server, agent_fleet, node, agent_bundle)

    keypair = bootstrap.control_plane_keypair(agent_server)
    private = keypair.private_openssh.decode()
    authorized = node.read(f"/home/{USER}/.ssh/authorized_keys", USER).decode()
    assert keypair.public_openssh.split()[1] in authorized
    for entry in node.commands:
        assert private not in (entry["command"] or "")
        assert private not in (entry["stdin"] or "")
    for upload in node.uploads:
        assert upload["size"] < len(private) or "authorized" not in upload["path"]


async def test_an_operator_supplied_key_asks_for_no_password_at_all(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    pair = generate_keypair("operator")
    node = make_node(tmp_path, password=None)
    node.users[USER].authorized_keys.add(pair.public_openssh)
    agent_fleet.add(node)

    async def never(_question):  # pragma: no cover - must not be called
        raise AssertionError("a password was asked for despite a key being supplied")

    report = await install_agent(
        agent_server,
        host=node.host,
        username=USER,
        control_host="127.0.0.1",
        connector=agent_fleet,
        bundle=agent_bundle,
        private_key=pair.private_openssh,
        confirm_host_key=confirm,
        password_prompt=never,
    )
    assert report.connected
    assert report.used_password is False
    assert report.key_generated is False


# ── The capability matrix ───────────────────────────────────────────────────


async def test_linger_off_is_enabled_with_one_privileged_call(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path, linger=False)
    report = await do_install(agent_server, agent_fleet, node, agent_bundle)

    assert report.scope == "user"
    assert report.connected
    assert node.linger[USER] is True
    assert [call["why"] for call in report.privileged_calls] == [
        f"enable lingering for {USER}"
    ]
    assert node.sudo_prompts == 1
    assert report.concessions == []


async def test_linger_off_and_no_sudo_password_is_a_named_concession(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """Declining is allowed. What it costs is said out loud.

    Entered with a key rather than a password, because a login password we
    already hold is reused for sudo rather than asking the operator twice for
    the same secret — so declining is only reachable on a key-authenticated
    node.
    """
    pair = generate_keypair("operator")
    node = make_node(tmp_path, host="10.0.0.8", password=None, linger=False)
    node.users[USER].authorized_keys.add(pair.public_openssh)
    agent_fleet.add(node)
    report = await install_agent(
        agent_server,
        host=node.host,
        username=USER,
        control_host="127.0.0.1",
        connector=agent_fleet,
        bundle=agent_bundle,
        private_key=pair.private_openssh,
        confirm_host_key=confirm,
        sudo_password_prompt=password_prompt(None),
    )
    assert report.connected
    assert node.linger.get(USER) is not True
    concessions = {c.capability for c in report.concessions}
    assert "linger" in concessions
    linger = next(c for c in report.concessions if c.capability == "linger")
    assert "logs out" in linger.cost
    assert node.sudo_prompts == 0


async def test_passwordless_sudo_is_used_without_asking_for_anything(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path, linger=False, sudo=SudoPolicy(mode="nopasswd"))

    async def never(_question):  # pragma: no cover
        raise AssertionError("a sudo password was asked for on a NOPASSWD node")

    report = await do_install(
        agent_server, agent_fleet, node, agent_bundle, sudo_password_prompt=never
    )
    assert report.connected
    assert node.linger[USER] is True
    assert node.sudo_prompts == 0
    assert [call["via"] for call in report.privileged_calls] == ["sudo -n"]


async def test_a_scoped_nopasswd_list_is_not_mistaken_for_working_sudo(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """The configuration that fails halfway through if it is misread.

    ``sudo -n -l`` exits 0 here, and the only thing it is free for is a
    ``systemctl`` this install does not need. Reading that as "sudo works"
    would send ``loginctl enable-linger`` through ``sudo -n``, get a password
    prompt nobody answers, and stop with the node already changed.
    """
    node = make_node(
        tmp_path,
        linger=False,
        sudo=SudoPolicy(
            mode="scoped",
            commands=("/usr/bin/systemctl restart spark-pulse-agent.service",),
            password=SUDO_PASSWORD,
        ),
    )
    agent_fleet.add(node)
    caps = await probe_node(
        await agent_fleet.connect(node.host, USER, password=PASSWORD), username=USER
    )
    assert caps.sudo.permitted is True
    assert caps.sudo.passwordless_all is False
    assert caps.sudo.password_required is True
    assert caps.sudo.free_for("loginctl enable-linger alex") is False
    assert (
        caps.sudo.free_for("/usr/bin/systemctl restart spark-pulse-agent.service")
        is True
    )

    report = await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert report.connected
    assert node.linger[USER] is True
    # It fell through to the password, which is the only thing that works here.
    assert [call["via"] for call in report.privileged_calls] == ["sudo -S"]


async def test_a_wrong_sudo_password_is_named(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    from spark_pulse.agent.bootstrap_probe import SudoAuthFailed

    pair = generate_keypair("operator")
    node = make_node(tmp_path, password=None, linger=False)
    node.users[USER].authorized_keys.add(pair.public_openssh)
    agent_fleet.add(node)
    with pytest.raises(SudoAuthFailed):
        await install_agent(
            agent_server,
            host=node.host,
            username=USER,
            control_host="127.0.0.1",
            connector=agent_fleet,
            bundle=agent_bundle,
            private_key=pair.private_openssh,
            confirm_host_key=confirm,
            sudo_password_prompt=password_prompt("wrong"),
        )


async def test_sudo_is_authenticated_on_every_call_not_once(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """tty_tickets: each exec channel is its own session, so each call pays.

    Two privileged actions are needed here — lingering and the docker group —
    and both must authenticate. An installer that assumed one authentication
    covered the install would show one prompt and fail on the second action.
    """
    node = make_node(tmp_path, linger=False, docker_socket=False)
    report = await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert node.sudo_prompts == 2
    assert [call["via"] for call in report.privileged_calls] == ["sudo -S", "sudo -S"]


async def test_a_sudo_password_that_differs_from_the_login_password_is_asked_for(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """Sudo does not have to use the login password — LDAP or SSSD routinely
    does not — so a refusal is a question, not a failure."""
    asked: list[str] = []

    async def prompt(question: str) -> str:
        asked.append(question)
        return SUDO_PASSWORD

    node = make_node(tmp_path, linger=False, sudo_password=SUDO_PASSWORD)
    report = await do_install(
        agent_server, agent_fleet, node, agent_bundle, sudo_password_prompt=prompt
    )
    assert report.connected
    assert node.linger[USER] is True
    # Asked once, after the login password was refused — not twice, and not
    # before there was any reason to.
    assert len(asked) == 1
    assert "refused" in asked[0]


async def test_requiretty_is_detected_rather_than_waited_on(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path, linger=False)
    node.requiretty = True
    agent_fleet.add(node)
    session = await agent_fleet.connect(node.host, USER, password=PASSWORD)
    caps = await probe_node(session, username=USER)
    assert caps.sudo.requiretty is True
    assert "tty" in caps.sudo.detail

    report = await do_install(agent_server, agent_fleet, node, agent_bundle)
    # Still installs — a user unit needs no sudo — and says what it lost.
    assert report.connected
    assert any(c.capability == "linger" for c in report.concessions)


async def test_no_docker_socket_is_a_concession_with_a_cost(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path, docker_socket=False, groups=("adm", "sudo"))
    report = await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert report.connected
    docker = next(c for c in report.concessions if c.capability == "docker")
    assert "docker group" in docker.detail
    assert "next login" in docker.cost
    assert "docker" in node.users[USER].groups


async def test_a_docker_daemon_that_is_down_is_not_read_as_no_permission(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path, docker_running=False)
    agent_fleet.add(node)
    caps = await probe_node(
        await agent_fleet.connect(node.host, USER, password=PASSWORD), username=USER
    )
    assert caps.docker_socket is False
    assert "Cannot connect to the Docker daemon" in caps.docker_error
    # The group is intact; this is a daemon problem, not a credentials one.
    assert caps.in_docker_group is True


async def test_no_user_manager_falls_back_to_a_system_unit(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path, user_manager=False, sudo=SudoPolicy(mode="nopasswd"))
    report = await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert report.scope == "system"
    assert "no systemd --user manager" in report.scope_reason
    assert report.connected
    assert node.exists("/etc/systemd/system/spark-pulse-agent.service")


async def test_an_explicitly_requested_scope_is_never_quietly_downgraded(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path, user_manager=False, sudo=SudoPolicy(mode="nopasswd"))
    agent_fleet.add(node)
    with pytest.raises(BootstrapError) as caught:
        await install_agent(
            agent_server,
            host=node.host,
            username=USER,
            control_host="127.0.0.1",
            connector=agent_fleet,
            bundle=agent_bundle,
            scope="user",
            confirm_host_key=confirm,
            password_prompt=password_prompt(PASSWORD),
        )
    assert "no systemd --user manager" in str(caught.value)
    assert "nothing was installed" in str(caught.value)


async def test_a_node_with_no_way_to_run_anything_says_which(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(
        tmp_path,
        user_manager=False,
        system_manager=False,
        sudo=SudoPolicy(mode="none"),
    )
    agent_fleet.add(node)
    with pytest.raises(BootstrapError) as caught:
        await install_agent(
            agent_server,
            host=node.host,
            username=USER,
            control_host="127.0.0.1",
            connector=agent_fleet,
            bundle=agent_bundle,
            confirm_host_key=confirm,
            password_prompt=password_prompt(PASSWORD),
        )
    assert "neither a user unit" in str(caught.value)


async def test_root_installs_a_system_unit_with_no_sudo(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    pair = generate_keypair("operator")
    node = SimulatedNode(
        HOST,
        tmp_path / "rootnode",
        users={
            "root": SimulatedUser(
                name="root",
                uid=0,
                authorized_keys={pair.public_openssh},
                groups=("root",),
            )
        },
        docker_socket_users={"root"},
    )
    agent_fleet.add(node)
    report = await install_agent(
        agent_server,
        host=HOST,
        username="root",
        control_host="127.0.0.1",
        connector=agent_fleet,
        bundle=agent_bundle,
        private_key=pair.private_openssh,
        confirm_host_key=confirm,
    )
    assert report.scope == "system"
    assert report.scope_reason == "the login user is root"
    assert report.connected
    assert sudo_elevations(node) == []


# ── Identity: converge, or refuse loudly ────────────────────────────────────


async def test_reinstalling_converges_on_the_identity_the_node_has(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    first = await do_install(agent_server, agent_fleet, node, agent_bundle)
    identity = node.read(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
    )

    second = await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert second.converged is True
    assert second.node_id == first.node_id
    assert second.connected
    assert (
        node.read(
            f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
        )
        == identity
    )


async def test_an_identity_this_control_plane_does_not_know_is_refused_loudly(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """A node from another cluster. Not ignored, the way k0s ignores its token."""
    node = make_node(tmp_path)
    await do_install(agent_server, agent_fleet, node, agent_bundle)

    # The control plane's memory of it is gone — a rebuilt ledger, or a node
    # that belonged to somebody else's cluster.
    identity_path = node.path(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
    )
    import json

    data = json.loads(identity_path.read_text())
    data["node_id"] = "00000000-0000-4000-8000-000000000000"
    identity_path.write_text(json.dumps(data))

    with pytest.raises(ExistingIdentity) as caught:
        await do_install(agent_server, agent_fleet, node, agent_bundle)
    message = str(caught.value)
    assert "00000000-0000-4000-8000-000000000000" in message
    assert "remove_node_and_identity" in message


# ── Installed, but never dialled home ───────────────────────────────────────


async def test_an_agent_that_never_dials_home_is_reported_not_claimed(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path, agent_dials_home=False)
    report = await do_install(
        agent_server, agent_fleet, node, agent_bundle, connect_timeout=1.0
    )
    assert report.node_id  # it enrolled: the token was spent
    assert report.connected is False
    dial = next(c for c in report.concessions if c.capability == "dial-home")
    assert "has not appeared in the hub" in dial.detail
    assert "systemctl" in dial.cost


# ── Uninstall and remove are two different things ───────────────────────────


async def test_uninstall_keeps_the_identity_and_reinstall_rejoins_as_the_same_node(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    first = await do_install(agent_server, agent_fleet, node, agent_bundle)
    access = NodeAccess(host=node.host, username=USER)

    removed = await uninstall_agent_keep_identity(
        agent_server, access, connector=agent_fleet
    )
    assert removed.node_id == first.node_id
    assert not node.exists(f"/home/{USER}/.local/share/spark-pulse/agent/current", USER)
    assert not node.exists(
        f"/home/{USER}/.config/systemd/user/spark-pulse-agent.service", USER
    )
    # The identity is exactly what "keep identity" means.
    assert node.exists(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
    )
    assert agent_server.ledger.get(first.node_id) is not None

    again = await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert again.node_id == first.node_id
    assert again.converged is True


async def test_remove_wipes_the_identity_and_the_next_install_is_a_new_node(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    first = await do_install(agent_server, agent_fleet, node, agent_bundle)
    access = NodeAccess(host=node.host, username=USER)

    await remove_node_and_identity(agent_server, access, connector=agent_fleet)
    assert not node.exists(
        f"/home/{USER}/.local/share/spark-pulse/agent-identity/identity.json", USER
    )
    assert agent_server.ledger.get(first.node_id) is None

    again = await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert again.node_id != first.node_id
    assert again.converged is False


# ── Secrets ─────────────────────────────────────────────────────────────────


async def test_no_secret_reaches_a_log_record_or_a_command(
    agent_server, agent_fleet, agent_bundle, tmp_path, caplog
):
    """The assertion this whole design exists to make possible.

    Neither the node password nor the sudo password nor the control plane's
    private key appears in any log record emitted anywhere in the process, in
    any command line sent to the node, or in the enrolment token file left on
    disk. The password may appear in exactly one place: the stdin of a
    ``sudo -S``, which is where §3.1 says to put it.
    """
    node = make_node(
        tmp_path, linger=False, docker_socket=False, sudo_password=SUDO_PASSWORD
    )
    with caplog.at_level(logging.DEBUG):
        report = await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert report.connected

    private = bootstrap.control_plane_keypair(agent_server).private_openssh.decode()
    secrets = [PASSWORD, SUDO_PASSWORD, private]

    for record in caplog.records:
        rendered = record.getMessage() + repr(record.args)
        for secret in secrets:
            assert secret not in rendered, f"{secret!r} leaked into {record.name}"

    for entry in node.commands:
        for secret in secrets:
            assert secret not in entry["command"], f"{secret!r} reached a command line"

    # And it did travel — on stdin, exactly once per privileged call.
    stdins = [entry["stdin"] for entry in node.commands if entry["stdin"]]
    assert any(SUDO_PASSWORD in (value or "") for value in stdins)
    assert all(private not in (value or "") for value in stdins)

    # Nothing secret is left on the node's disk either.
    for path in node.root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for secret in secrets:
            assert secret not in text, f"{secret!r} was written to {path}"


async def test_the_report_carries_the_probe_results(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """A node none of us has seen has to be explainable from the report alone."""
    node = make_node(tmp_path)
    report = await do_install(agent_server, agent_fleet, node, agent_bundle)
    caps = report.capabilities
    assert caps["user"] == USER
    assert caps["docker_socket"] is True
    assert caps["linger"] is True
    assert caps["user_manager"] is True
    assert "sudo" in caps and "detail" in caps["sudo"]
    assert report.scope_reason
    assert report.bundle["version"]
    assert any("probed" in step for step in report.steps)
