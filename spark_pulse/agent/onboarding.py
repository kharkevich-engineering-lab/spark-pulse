"""Onboarding a node from the browser: one request, one install, no residue.

:func:`spark_pulse.agent.bootstrap.install_agent` is the installer. It was
written for an operator at a terminal — it *asks* for a password, *asks* for a
sudo password, *shows* a host key and waits for a yes — and until this module
nothing called it: the Cluster page could register a node's address and could
not put an agent on it. This is the adapter between a browser form and those
prompts, and it is deliberately small so the rules are all in one place:

* **Secrets arrive in the request body and live for the length of the call.**
  A password, a private key, a passphrase and a sudo password are accepted;
  none is written to the node registry, the ledger, a log line or the report
  handed back. The report says *that* a password was used, never which.
* **The host key is confirmed in two steps, like ``ssh`` does.** The browser
  first asks :func:`host_key_of` and shows the fingerprint; the install then
  carries the fingerprint the operator saw, and refuses if the node offers a
  different one. Nothing secret is sent before that comparison passes.
* **An operator's key is unlocked here, not on the node.** A passphrase-
  protected key is decrypted in this process and the SSH library is handed the
  clear key; the passphrase itself goes no further than
  :func:`keypair_from_private_pem`.
* **Whichever key opened the door, the control plane's own key is left
  behind.** Every later SSH to the node — a model rsync, a reinstall — uses
  the control plane's key, so an install with the operator's key that did not
  install ours would work once and never again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from spark_pulse.agent.bootstrap import (
    control_plane_keypair,
    install_agent,
)
from spark_pulse.agent.bootstrap_transport import (
    AsyncSSHConnector,
    Connector,
    HostKey,
    HostKeyDeclined,
    keypair_from_private_pem,
)
from spark_pulse.agent.bundle import AgentBundle
from spark_pulse.agent.server import ControlPlaneServer

logger = logging.getLogger(__name__)

__all__ = [
    "AUTH_METHODS",
    "bundle_factory",
    "connector_factory",
    "OnboardRequest",
    "host_key_of",
    "onboard",
    "parse_request",
]

#: How the installer may authenticate to the node. ``password`` pushes the
#: control plane's key and verifies it before anything else; ``key`` is a
#: private key the operator supplied; ``control_plane_key`` is for a node whose
#: ``authorized_keys`` already holds this control plane's public key — a
#: reinstall, or an operator who copied it there by hand.
AUTH_METHODS = ("password", "key", "control_plane_key")

#: Where the SSH connection and the agent bundle come from when a caller does
#: not say. The router never says, so a test that must not open a socket or
#: cross-compile an agent replaces these two for its duration and nothing else.
connector_factory: Callable[[], Connector] = AsyncSSHConnector


def _default_bundle() -> AgentBundle | None:
    return None


bundle_factory: Callable[[], AgentBundle | None] = _default_bundle


@dataclass(frozen=True)
class OnboardRequest:
    """One install, as the browser asked for it. Secrets included, by design."""

    username: str
    auth: str
    host_key_fingerprint: str
    password: str | None = None
    private_key: bytes | None = None
    passphrase: str | None = None
    sudo_password: str | None = None
    port: int = 22
    scope: str = "auto"
    control_host: str = ""


def parse_request(body: dict[str, Any]) -> OnboardRequest:
    """Validate a request body. Raises :class:`ValueError` naming the field."""
    username = str(body.get("username") or "").strip()
    if not username:
        raise ValueError("username is required")
    auth = str(body.get("auth") or "").strip()
    if auth not in AUTH_METHODS:
        raise ValueError(f"auth must be one of {', '.join(AUTH_METHODS)}")
    fingerprint = str(body.get("host_key_fingerprint") or "").strip()
    if not fingerprint:
        raise ValueError(
            "host_key_fingerprint is required: fetch the node's host key first and "
            "confirm the fingerprint it shows"
        )
    password = body.get("password")
    private_key = body.get("private_key")
    if auth == "password" and not password:
        raise ValueError("a password is required for password authentication")
    if auth == "key" and not private_key:
        raise ValueError("a private key is required for key authentication")
    raw_port = body.get("port")
    try:
        port = 22 if raw_port in (None, "") else int(raw_port)
    except (TypeError, ValueError):
        raise ValueError("port must be a number") from None
    if not 0 < port < 65536:
        raise ValueError("port must be between 1 and 65535")
    scope = str(body.get("scope") or "auto")
    if scope not in ("auto", "user", "system"):
        raise ValueError("scope must be auto, user or system")
    return OnboardRequest(
        username=username,
        auth=auth,
        host_key_fingerprint=fingerprint,
        password=str(password) if auth == "password" else None,
        private_key=str(private_key).encode() if auth == "key" else None,
        passphrase=(str(body.get("passphrase")) if body.get("passphrase") else None),
        sudo_password=(
            str(body.get("sudo_password")) if body.get("sudo_password") else None
        ),
        port=port,
        scope=scope,
        control_host=str(body.get("control_host") or "").strip(),
    )


async def host_key_of(
    host: str, port: int = 22, *, connector: Connector | None = None
) -> HostKey:
    """The host key ``host`` offers, for the operator to confirm before a secret moves."""
    connector = connector or connector_factory()
    return await connector.host_key(host, port)


async def onboard(
    server: ControlPlaneServer,
    request: OnboardRequest,
    *,
    host: str,
    control_host: str,
    name: str = "",
    node_id: str = "",
    connector: Connector | None = None,
    bundle: AgentBundle | None = None,
) -> dict[str, Any]:
    """Install the agent on ``host`` as the request describes, and report.

    ``node_id`` pins the identity to the registry's, so the record the operator
    added and the agent that enrols are one node. The returned dict is
    :class:`InstallReport` as a dict, with nothing secret in it.
    """
    expected = request.host_key_fingerprint

    async def confirm(offered: HostKey) -> bool:
        if offered.fingerprint == expected:
            return True
        raise HostKeyDeclined(
            f"{host}:{request.port} now offers host key {offered.fingerprint}, "
            f"not the {expected} that was confirmed; nothing was sent. Fetch the "
            "host key again and check the machine before trusting it."
        )

    async def password(_question: str) -> str | None:
        return request.password

    async def sudo_password(_question: str) -> str | None:
        return request.sudo_password

    private_key: bytes | None = None
    if request.auth == "key":
        assert request.private_key is not None  # parse_request guarantees it
        private_key = keypair_from_private_pem(
            request.private_key, passphrase=request.passphrase
        ).private_openssh
    elif request.auth == "control_plane_key":
        private_key = control_plane_keypair(server).private_openssh

    report = await install_agent(
        server,
        host=host,
        username=request.username,
        control_host=control_host,
        name=name,
        node_id=node_id,
        port=request.port,
        connector=connector or connector_factory(),
        private_key=private_key,
        confirm_host_key=confirm,
        password_prompt=password if request.auth == "password" else None,
        sudo_password_prompt=sudo_password,
        scope=request.scope,
        bundle=bundle or bundle_factory(),
    )
    return report.to_dict()
