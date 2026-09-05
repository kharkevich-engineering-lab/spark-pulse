"""Building a node to install onto, shared by the installer and doctor suites.

The fixtures live in ``tests/conftest.py`` (``agent_server``, ``agent_fleet``,
``agent_bundle``) rather than here, so neither suite has to import a fixture by
name and then shadow it with a parameter of the same name.
"""

from __future__ import annotations

from spark_pulse.agent.bootstrap import install_agent
from spark_pulse.mock.bootstrap_node import (
    InProcessAgentRunner,
    SimulatedNode,
    SimulatedUser,
    SudoPolicy,
)

HOST = "10.0.0.7"
USER = "alex"
PASSWORD = "correct-horse-battery-staple"
SUDO_PASSWORD = "a-different-sudo-secret"


def make_node(
    tmp_path,
    *,
    host: str = HOST,
    user: str = USER,
    password: str | None = PASSWORD,
    uid: int = 1000,
    groups: tuple[str, ...] = ("adm", "sudo", "docker"),
    sudo: SudoPolicy | None = None,
    sudo_password: str | None = None,
    docker_socket: bool = True,
    docker_running: bool = True,
    linger: bool | None = True,
    loginctl: bool = True,
    linger_settable: bool = True,
    user_manager: bool = True,
    system_manager: bool = True,
    agent_dials_home: bool = True,
    runner=None,
) -> SimulatedNode:
    """One point of the matrix, with every dimension named at the call site."""
    account = SimulatedUser(
        name=user,
        uid=uid,
        password=password,
        groups=groups,
        sudo=sudo or SudoPolicy(mode="password", password=sudo_password),
    )
    return SimulatedNode(
        host,
        tmp_path / f"node-{host}",
        users={user: account},
        docker_socket_users={user} if docker_socket else set(),
        docker_running=docker_running,
        linger={} if linger is None else {user: linger},
        loginctl=loginctl,
        linger_settable=linger_settable,
        user_manager=user_manager,
        system_manager=system_manager,
        agent_dials_home=agent_dials_home,
        agent_runner=runner or InProcessAgentRunner(),
    )


async def confirm(_host_key) -> bool:
    return True


async def decline(_host_key) -> bool:
    return False


def password_prompt(value: str | None):
    """A prompt that answers ``value``. ``None`` is the operator declining."""

    async def prompt(_question: str) -> str | None:
        return value

    return prompt


async def do_install(agent_server, agent_fleet, node, agent_bundle, **kwargs):
    """Install onto ``node`` with the usual answers, overridable per test."""
    agent_fleet.add(node)
    kwargs.setdefault("confirm_host_key", confirm)
    kwargs.setdefault("password_prompt", password_prompt(PASSWORD))
    kwargs.setdefault("sudo_password_prompt", password_prompt(SUDO_PASSWORD))
    return await install_agent(
        agent_server,
        host=node.host,
        username=next(iter(node.users)),
        control_host="127.0.0.1",
        name=kwargs.pop("name", "worker-1"),
        connector=agent_fleet,
        bundle=agent_bundle,
        **kwargs,
    )


def sudo_elevations(node: SimulatedNode) -> list[str]:
    """Every sudo that ran something, excluding the read-only ``sudo -n -l``.

    The probe is not an elevation: it enumerates policy and changes nothing.
    Counting it as one would make "this install needed no root" untestable.
    """
    return [
        entry["command"]
        for entry in node.commands
        if entry["command"].startswith("sudo ") and not entry["command"].endswith(" -l")
    ]
