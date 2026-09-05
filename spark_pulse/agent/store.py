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

from spark_pulse.agent import agent_pb2 as pb
from spark_pulse.agent.identity import spki_pin

logger = logging.getLogger(__name__)

__all__ = ["AgentIdentity", "default_identity_dir"]


def default_identity_dir() -> Path:
    """``~/.config/spark-pulse/agent``, overridable for tests and for systemd."""
    override = os.environ.get("SPARK_PULSE_AGENT_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "spark-pulse" / "agent"


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
    def load(cls, directory: Path | str) -> AgentIdentity | None:
        """The identity in ``directory``, or None if the node has none.

        A *partial* directory — some files but not all — raises rather than
        returning None. Half an identity is a failed install, and answering
        "this node has never enrolled" to it would let the installer enroll it
        a second time and orphan the first uuid.
        """
        directory = Path(directory)
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
        """Write the identity out, with the key 0600 from the moment it exists."""
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        paths = self.paths(self.directory)
        fd = os.open(str(paths["key"]), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(self.key_pem)
        paths["cert"].write_bytes(self.certificate_pem)
        paths["bundle"].write_bytes(self.trust_bundle_pem)
        paths["meta"].write_text(
            json.dumps(
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
        )

    def destroy(self) -> None:
        """The *Remove* action: wipe the identity. Re-enrollment is required."""
        for path in self.paths(self.directory).values():
            path.unlink(missing_ok=True)

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

    def update_from(self, identity: pb.Identity) -> None:
        """Adopt a freshly issued certificate, keeping the key.

        Called on renewal. The pin is *checked*, not replaced: a renewal that
        arrives carrying a different trust bundle is the one thing a pin
        exists to catch, and adopting it would delete the protection.
        """
        if not self.verify_pin(identity.trust_bundle_pem):
            raise RuntimeError(
                "the trust bundle offered on renewal does not match the pin "
                "recorded at enrollment; refusing it"
            )
        self.certificate_pem = identity.certificate_pem
        self.trust_bundle_pem = identity.trust_bundle_pem
        self.not_before = float(identity.not_before_unix)
        self.not_after = float(identity.not_after_unix)
        self.epoch = int(identity.epoch)
        self.save()

    @property
    def not_before_dt(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.not_before, dt.timezone.utc)

    @property
    def not_after_dt(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.not_after, dt.timezone.utc)
