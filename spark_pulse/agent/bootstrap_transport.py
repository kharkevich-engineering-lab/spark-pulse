"""The SSH channel the installer runs over, and the one real implementation.

The installer in :mod:`spark_pulse.agent.bootstrap` never imports ``asyncssh``
and never builds an ``ssh`` argv. It talks to :class:`NodeSession`, which has
three methods, and that is the whole surface a simulated node has to answer.
Everything §3.1 of ``docs/cluster-agent-plan.md`` forbids is forbidden *here*,
once, rather than being remembered at each of the twenty places the installer
runs a command:

* **A password never reaches argv.** :meth:`NodeSession.run` takes ``stdin``
  as a separate argument, and there is no other way to hand bytes to a remote
  command. ``sshpass`` is not used, because a password is never handed to an
  ``ssh`` process at all — :class:`AsyncSSHConnector` opens the connection
  in-process and the password is a Python string that is dropped when the
  install ends.
* **A private key never leaves the control plane.** :func:`generate_keypair`
  returns both halves and only :attr:`KeyPair.public_openssh` is ever sent.
  NVIDIA's ``discover-sparks`` copies one *private* key to every node so that
  access is bidirectional, which makes any single compromised Spark a key to
  the whole fabric; §3.1 rejects that explicitly, and this type is the shape
  of the rejection.
* **The host key is available before authentication.**
  :meth:`Connector.host_key` reaches the node far enough to learn its key and
  then drops the connection, so a fingerprint can be shown while nothing
  secret has moved. :meth:`Connector.connect` then *pins* the key that was
  confirmed, so the fingerprint the operator approved is the one the session
  is established against.

``asyncssh`` is imported inside :meth:`AsyncSSHConnector._asyncssh` rather
than at module scope. The suite drives a simulated fleet and must not need an
SSH library to do it, and a control plane that will never install an agent
must not fail to import because one is missing.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

__all__ = [
    "AsyncSSHConnector",
    "AuthFailed",
    "BootstrapError",
    "Connector",
    "HostKey",
    "HostKeyConfirm",
    "HostKeyDeclined",
    "KeyPair",
    "NodeSession",
    "Prompt",
    "RootPasswordBootstrap",
    "RunResult",
    "Unreachable",
    "generate_keypair",
    "keypair_from_private_pem",
]


# ── Errors ──────────────────────────────────────────────────────────────────


class BootstrapError(Exception):
    """Anything that stops an install, with a message an operator can act on."""


class Unreachable(BootstrapError):
    """The node did not answer on the SSH port."""


class AuthFailed(BootstrapError):
    """The node answered and refused the credentials offered."""


class RootPasswordBootstrap(AuthFailed):
    """A password bootstrap as ``root``, which Ubuntu 24.04 cannot accept.

    ``PermitRootLogin`` defaults to ``prohibit-password`` there, so the node
    refuses a password for root however correct it is, and DGX OS onboarding
    creates an ordinary sudo user rather than enabling root. §3.1 names this
    as one of two details that otherwise costs a day, because the failure is
    indistinguishable from a wrong password unless something says so.
    """


class HostKeyDeclined(BootstrapError):
    """The operator did not confirm the host key. Nothing was sent."""


# ── Values ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HostKey:
    """A server host key, as it is shown to an operator before anything is sent."""

    host: str
    port: int
    algorithm: str
    #: The wire-format public key blob, already base64-decoded.
    blob: bytes

    @property
    def fingerprint(self) -> str:
        """``SHA256:…``, byte-for-byte what ``ssh-keygen -lf`` prints."""
        digest = hashlib.sha256(self.blob).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")

    @property
    def openssh(self) -> str:
        """The ``<algorithm> <base64>`` half of a known_hosts line."""
        return f"{self.algorithm} {base64.b64encode(self.blob).decode()}"

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.host}:{self.port} {self.algorithm} {self.fingerprint}"


@dataclass(frozen=True)
class RunResult:
    """What a remote command did. ``returncode`` is the command's own."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class KeyPair:
    """An SSH keypair. Only :attr:`public_openssh` is ever sent to a node."""

    private_openssh: bytes
    public_openssh: str

    @property
    def fingerprint(self) -> str:
        parts = self.public_openssh.split()
        digest = hashlib.sha256(base64.b64decode(parts[1])).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def generate_keypair(comment: str = "spark-pulse") -> KeyPair:
    """A fresh ed25519 keypair, generated on the control plane and kept there."""
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return KeyPair(private, f"{public.decode()} {comment}".strip())


def keypair_from_private_pem(
    private_pem: bytes, comment: str = "spark-pulse"
) -> KeyPair:
    """Derive the public half of a private key the operator supplied.

    The private bytes are carried so the installer can authenticate *from* the
    control plane with them. They are never a thing any method here sends.
    """
    key = serialization.load_ssh_private_key(private_pem, password=None)
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return KeyPair(private_pem, f"{public.decode()} {comment}".strip())


# ── The channel ─────────────────────────────────────────────────────────────


@runtime_checkable
class NodeSession(Protocol):
    """One authenticated connection to one node."""

    async def run(
        self,
        command: str,
        *,
        stdin: str | None = None,
        timeout: float = 60.0,
    ) -> RunResult:
        """Run ``command``, optionally writing ``stdin`` to it.

        ``stdin`` is the *only* way a secret reaches a remote command. A
        caller needing ``sudo`` passes the password here and ``sudo -S`` in
        the command; nothing secret is ever interpolated into the text.
        """
        ...

    async def upload(self, data: bytes, remote_path: str, *, mode: int = 0o600) -> None:
        """Write ``data`` to ``remote_path``, created with ``mode`` from the start."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class Connector(Protocol):
    """Opens sessions. The installer holds one of these and nothing else."""

    async def host_key(
        self, host: str, port: int = 22, *, timeout: float = 10.0
    ) -> HostKey:
        """Learn the node's host key without authenticating to it."""
        ...

    async def connect(
        self,
        host: str,
        username: str,
        *,
        port: int = 22,
        password: str | None = None,
        private_key: bytes | None = None,
        host_key: HostKey | None = None,
        timeout: float = 20.0,
    ) -> NodeSession:
        """Authenticate, pinning ``host_key`` when one was confirmed."""
        ...


#: Shown a fingerprint, answers whether to proceed. Called before any secret.
HostKeyConfirm = Callable[[HostKey], Awaitable[bool]]

#: Asks the operator for a secret. Returning ``None`` means "I decline".
Prompt = Callable[[str], Awaitable[str | None]]


# ── asyncssh ────────────────────────────────────────────────────────────────


class _AsyncSSHSession:
    """A :class:`NodeSession` over one ``asyncssh`` connection."""

    def __init__(self, connection: Any):
        self._connection = connection

    async def run(
        self,
        command: str,
        *,
        stdin: str | None = None,
        timeout: float = 60.0,
    ) -> RunResult:
        import asyncio

        try:
            result = await asyncio.wait_for(
                self._connection.run(command, input=stdin, check=False),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # A sudo that met ``requiretty`` writes its prompt to a tty that
            # does not exist and waits forever. Timing out and naming the
            # command is the difference between a diagnosable install and a
            # hung one; the command text never carries a secret, by design.
            raise BootstrapError(
                f"remote command timed out after {timeout:g}s: {command[:200]}"
            )
        return RunResult(
            returncode=int(result.exit_status or 0),
            stdout=_text(result.stdout),
            stderr=_text(result.stderr),
        )

    async def upload(self, data: bytes, remote_path: str, *, mode: int = 0o600) -> None:
        async with self._connection.start_sftp_client() as sftp:
            async with sftp.open(remote_path, "wb") as handle:
                await handle.write(data)
            await sftp.chmod(remote_path, mode)

    async def close(self) -> None:
        self._connection.close()
        await self._connection.wait_closed()


class AsyncSSHConnector:
    """The real connector: ``asyncssh``, in this process, no subprocess.

    The password never becomes an argument to anything, because there is no
    ``ssh`` child process for it to be an argument *of*.
    """

    @staticmethod
    def _asyncssh():
        try:
            import asyncssh
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise BootstrapError(
                "installing an agent over SSH needs the 'asyncssh' package; "
                "reinstall spark-pulse, or pip install asyncssh"
            ) from exc
        return asyncssh

    async def host_key(
        self, host: str, port: int = 22, *, timeout: float = 10.0
    ) -> HostKey:  # pragma: no cover - needs a real sshd
        import asyncio

        asyncssh = self._asyncssh()
        try:
            key = await asyncio.wait_for(
                asyncssh.get_server_host_key(host, port=port), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise Unreachable(f"{host}:{port} did not answer within {timeout:g}s")
        except OSError as exc:
            raise Unreachable(f"cannot reach {host}:{port}: {exc}") from exc
        if key is None:
            raise Unreachable(f"{host}:{port} offered no host key")
        return HostKey(
            host=host, port=port, algorithm=key.get_algorithm(), blob=key.public_data
        )

    async def connect(
        self,
        host: str,
        username: str,
        *,
        port: int = 22,
        password: str | None = None,
        private_key: bytes | None = None,
        host_key: HostKey | None = None,
        timeout: float = 20.0,
    ) -> NodeSession:  # pragma: no cover - needs a real sshd
        import asyncio

        asyncssh = self._asyncssh()
        options: dict[str, Any] = {
            "username": username,
            "port": port,
            # Never fall back to whatever the operator's own ssh-agent holds.
            # An install that quietly succeeded on an unrelated key leaves a
            # node nobody can reach the day that key is gone.
            "agent_path": None,
            "known_hosts": self._known_hosts(asyncssh, host_key),
            "client_keys": (
                [asyncssh.import_private_key(private_key)]
                if private_key is not None
                else []
            ),
        }
        if password is not None:
            options["password"] = password
        try:
            connection = await asyncio.wait_for(
                asyncssh.connect(host, **options), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise Unreachable(
                f"{host}:{port} did not complete a handshake in {timeout:g}s"
            )
        except asyncssh.PermissionDenied as exc:
            if username == "root" and password is not None and private_key is None:
                raise RootPasswordBootstrap(
                    ROOT_PASSWORD_MESSAGE.format(host=host)
                ) from exc
            raise AuthFailed(
                f"{username}@{host} refused the credentials offered"
            ) from exc
        except OSError as exc:
            raise Unreachable(f"cannot reach {host}:{port}: {exc}") from exc
        return _AsyncSSHSession(connection)

    def _known_hosts(self, asyncssh: Any, host_key: HostKey | None):
        if host_key is None:
            # asyncssh reads the tuple as "these keys are trusted"; an empty
            # set trusts nothing, which is the right answer for a connection
            # attempted without a confirmed fingerprint.
            return ()
        trusted = asyncssh.import_public_key(host_key.openssh)
        return lambda _host, _addr, _port: ([trusted], [], [])


#: Said whenever a password bootstrap as root is attempted or refused.
ROOT_PASSWORD_MESSAGE = (
    "{host} refused a password for root. Ubuntu 24.04 — and DGX OS with it — "
    "defaults PermitRootLogin to prohibit-password, so no root password can "
    "work over SSH, correct or not. Onboarding a Spark creates an ordinary "
    "user with sudo; install as that user, or supply a key for root."
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
