"""The identity a node keeps on disk.

Four files under one directory, and the directory is the identity:

* ``node.key`` — the private key, 0600, generated on the node and never sent.
* ``node.crt`` — the certificate the control plane issued for it.
* ``ca.pem`` — the trust bundle. Every authority this node will talk to.
* ``identity.json`` — the node id, the SPKI pin, the cluster id and the
  certificate window.

Removal versus uninstall, from §3.1, is exactly the presence of this
directory. **Uninstall, keep identity** leaves it, so reinstalling the agent
rejoins the cluster with the same uuid and the same certificate. **Remove**
deletes it, and the node must be enrolled again. k3s's uninstall script
removes its config but not its node identity, and that asymmetry is why
reinstall works there and reimaging does not; here the two actions are named
and separate, and :meth:`AgentIdentity.destroy` is the second one.

An installer finding this directory populated must converge or refuse loudly,
never silently ignore the token it was given — which is k0s's behaviour and
why re-enrollment there needs a full reset.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from spark_pulse.agent.identity import spki_pin

logger = logging.getLogger(__name__)

__all__ = ["AgentIdentity", "default_identity_dir"]


def default_identity_dir() -> Path:
    """``~/.config/spark-pulse/agent``, overridable for tests and for systemd."""
    override = os.environ.get("SPARK_PULSE_AGENT_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "spark-pulse" / "agent"


#: The four files, in the order a commit renames them into place.
FILES = ("ca.pem", "node.crt", "node.key", "identity.json")
#: What a file is staged as before a commit renames it into place.
STAGED = ".next"
#: Present from the moment every staged file is durable until every one has
#: been renamed into place: what tells a finished staging from a torn one.
COMMIT_MARKER = "identity.commit"


def _write_durable(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` with ``mode`` from creation, and make it durable."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_dir(directory: Path) -> None:
    """A rename is not on disk until the directory holding it is."""
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class AgentIdentity:
    """What a node needs to prove who it is."""

    directory: Path
    node_id: str
    key_pem: bytes
    certificate_pem: bytes
    trust_bundle_pem: bytes
    trust_bundle_pin: str
    cluster_id: str = ""
    spiffe_id: str = ""
    epoch: int = 0
    not_before: float = 0.0
    not_after: float = 0.0

    # ── Files ────────────────────────────────────────────────────────────

    @staticmethod
    def paths(directory: Path | str) -> dict[str, Path]:
        directory = Path(directory)
        return {
            "key": directory / "node.key",
            "cert": directory / "node.crt",
            "bundle": directory / "ca.pem",
            "meta": directory / "identity.json",
        }

    @classmethod
    def recover(cls, directory: Path | str) -> None:
        """Finish or discard a commit a crash interrupted. Idempotent.

        The same protocol as the Rust agent's ``identity::recover``: with
        :data:`COMMIT_MARKER` present every staged file was durable, so the
        commit is rolled forward; without it the staging never became the
        identity and is discarded.
        """
        directory = Path(directory)
        marker = directory / COMMIT_MARKER
        if marker.exists():
            for name in FILES:
                staged = directory / f"{name}{STAGED}"
                if staged.exists():
                    os.replace(staged, directory / name)
            _sync_dir(directory)
            marker.unlink()
            _sync_dir(directory)
            return
        for name in FILES:
            (directory / f"{name}{STAGED}").unlink(missing_ok=True)

    @classmethod
    def load(cls, directory: Path | str) -> AgentIdentity | None:
        """The identity in ``directory``, or None if the node has none.

        A *partial* directory — some files but not all — raises rather than
        returning None. Half an identity is a failed install, and answering
        "this node has never enrolled" to it would let the installer enroll it
        a second time and orphan the first uuid.
        """
        directory = Path(directory)
        if directory.is_dir():
            cls.recover(directory)
        paths = cls.paths(directory)
        present = {name: p for name, p in paths.items() if p.exists()}
        if not present:
            return None
        missing = sorted(set(paths) - set(present))
        if missing:
            raise RuntimeError(
                f"{directory} holds a partial agent identity; missing {missing}. "
                "Remove the directory to re-enroll, or restore the missing files."
            )
        meta = json.loads(paths["meta"].read_text())
        return cls(
            directory=directory,
            node_id=meta["node_id"],
            key_pem=paths["key"].read_bytes(),
            certificate_pem=paths["cert"].read_bytes(),
            trust_bundle_pem=paths["bundle"].read_bytes(),
            trust_bundle_pin=meta.get("trust_bundle_pin", ""),
            cluster_id=meta.get("cluster_id", ""),
            spiffe_id=meta.get("spiffe_id", ""),
            epoch=int(meta.get("epoch") or 0),
            not_before=float(meta.get("not_before") or 0.0),
            not_after=float(meta.get("not_after") or 0.0),
        )

    def save(self) -> None:
        """Write the identity out as one commit, the key 0600 from creation.

        Every file is staged and made durable, :data:`COMMIT_MARKER` says the
        staging is complete, then each is renamed into place — so a reader
        sees the old file or the new one, and a crash leaves a staging
        :meth:`recover` can finish or discard rather than a key that does not
        match its certificate. The Rust agent writes the same way.
        """
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self.recover(directory)
        meta = json.dumps(
            {
                "node_id": self.node_id,
                "trust_bundle_pin": self.trust_bundle_pin,
                "cluster_id": self.cluster_id,
                "spiffe_id": self.spiffe_id,
                "epoch": self.epoch,
                "not_before": self.not_before,
                "not_after": self.not_after,
            },
            indent=2,
        )
        contents = {
            "ca.pem": self.trust_bundle_pem,
            "node.crt": self.certificate_pem,
            "node.key": self.key_pem,
            "identity.json": meta.encode(),
        }
        for name, data in contents.items():
            mode = 0o600 if name == "node.key" else 0o644
            _write_durable(directory / f"{name}{STAGED}", data, mode)
        _sync_dir(directory)
        _write_durable(directory / COMMIT_MARKER, b"", 0o600)
        _sync_dir(directory)
        self.recover(directory)

    def destroy(self) -> None:
        """The *Remove* action: wipe the identity. Re-enrollment is required."""
        for path in self.paths(self.directory).values():
            path.unlink(missing_ok=True)
        for name in FILES:
            (self.directory / f"{name}{STAGED}").unlink(missing_ok=True)
        (self.directory / COMMIT_MARKER).unlink(missing_ok=True)

    # ── Checks ───────────────────────────────────────────────────────────

    def verify_pin(self, bundle_pem: bytes) -> bool:
        """Whether a bundle matches the pin recorded at enrollment.

        The pin is over the SPKI of every certificate in the bundle, so a CA
        renewed onto the same key still matches and a CA quietly added does
        not.
        """
        if not self.trust_bundle_pin:
            return True
        try:
            return spki_pin(bundle_pem) == self.trust_bundle_pin
        except Exception:
            return False

    @property
    def not_before_dt(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.not_before, dt.timezone.utc)

    @property
    def not_after_dt(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.not_after, dt.timezone.utc)
