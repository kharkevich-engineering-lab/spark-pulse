"""The names the control plane's listener certificate must answer to.

A node dials the control plane by whatever address the install handed it —
``192.168.29.60``, say — and the TLS client checks that name against the
server certificate before anything else happens. The certificate is minted
fresh at every start, and until this module it was minted for ``localhost``
and the loopback addresses only, which is exactly enough for the control
node's own agent and not one name more. The first peer to enrol was refused
with "certificate not valid for name", after the install had already put an
agent on it.

So the certificate is issued for every name this machine can plausibly be
reached by: its hostname (bare and ``.local``), every address on every
interface, the address the node registry holds for it, and loopback. Nothing
here is authorisation — the SPIFFE URI is what an agent trusts — these are
only the strings a dialler is allowed to have used. Over-including is
harmless; a missing one is an install that fails at the last step.

Best effort throughout: a discovery that raises loses its contribution, not
the certificate.
"""

from __future__ import annotations

import ipaddress
import logging
import socket

logger = logging.getLogger(__name__)

__all__ = ["advertised_names", "split_names"]


def split_names(candidates: list[str]) -> tuple[list[str], list[str]]:
    """Sort ``candidates`` into ``(dns_names, ip_addresses)``, deduplicated.

    An IP with a zone or prefix (``fe80::1%eth0``, ``10.0.0.1/24``) is
    reduced to the address; anything that is neither an address nor a
    plausible hostname is dropped rather than put into a certificate.
    """
    dns: list[str] = []
    ips: list[str] = []
    for raw in candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        bare = text.split("/", 1)[0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(bare)
        except ValueError:
            name = text.lower().rstrip(".")
            if _plausible_hostname(name) and name not in dns:
                dns.append(name)
            continue
        canonical = str(address)
        if canonical not in ips:
            ips.append(canonical)
    return dns, ips


def _plausible_hostname(name: str) -> bool:
    if not name or len(name) > 253:
        return False
    return all(
        label and len(label) <= 63 and all(c.isalnum() or c == "-" for c in label)
        for label in name.split(".")
    )


def advertised_names(extra: list[str] | None = None) -> tuple[list[str], list[str]]:
    """``(dns_names, ip_addresses)`` for this machine's listener certificate."""
    candidates: list[str] = ["localhost", "127.0.0.1", "::1"]

    try:
        host = socket.gethostname()
        if host:
            candidates.append(host)
            short = host.split(".", 1)[0]
            candidates.append(short)
            candidates.append(f"{short}.local")
    except OSError as exc:  # pragma: no cover — no hostname is a broken host
        logger.debug("no hostname for the listener certificate: %s", exc)

    try:
        from spark_pulse.tools import discovery

        for interface in discovery.detect_network_interfaces():
            if interface.ip:
                candidates.append(interface.ip)
        local = discovery.detect_local_ip()
        if local:
            candidates.append(local)
    except Exception as exc:
        logger.debug("interface discovery failed for the listener certificate: %s", exc)

    try:
        from spark_pulse.tools import node_registry

        me = node_registry.self_node()
        if me is not None and me.address:
            candidates.append(me.address)
    except Exception as exc:
        logger.debug("no registry address for the listener certificate: %s", exc)

    candidates.extend(extra or [])
    return split_names(candidates)
