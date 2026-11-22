"""The identity-relevant facts a node reports, as the ledger reads them.

Collecting facts is the *node's* job and lives in the agent — `agent/src/facts.rs`
reads them from `uname`, `sysconf`, `/sys/class/net` and the rest. What is left
here is the control plane's side: the three fields the enrolment ledger
compares on every connection, pulled out of the message the node sent.

Only these, and the reason each is here is worth keeping:

* ``machine_id`` is **diagnostic only**. DGX Sparks ship duplicates, so it may
  never be identity; the ledger holds it so it can warn that two nodes claim
  one, never to key on it.
* ``boot_id`` changes on every reboot, which is how a reboot is told from a
  reimage.
* ``stable_fingerprint`` is stable across reboots and unstable across a
  reimage. Compared against what enrolment recorded, so a machine that has been
  rebuilt under an already-accepted uuid is *surfaced for a human decision*
  rather than silently trusted or silently denied.
* ``hardware_fingerprint`` is the print before it, which also hashed the memory
  size — and a DGX Spark's kernel reports that one page differently from one
  boot to the next, so a reboot read as a reimage. It is compared only for an
  agent that sends no ``stable_fingerprint``; the agent still fills it so that
  a control plane rolled back to before the stable one finds what it recorded.
"""

from __future__ import annotations

from spark_pulse.agent import agent_pb2 as pb

__all__ = ["facts_dict"]


def facts_dict(facts: pb.NodeFacts) -> dict[str, str]:
    """The identity-relevant facts, as the ledger wants them."""
    return {
        "machine_id": facts.machine_id,
        "boot_id": facts.boot_id,
        "hardware_fingerprint": facts.hardware_fingerprint,
        "stable_fingerprint": facts.stable_fingerprint,
    }
