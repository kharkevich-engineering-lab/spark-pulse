"""Writing a node's fabric plan onto the node, and proving it took.

The plan is pure; this is the part that logs in. It uses the same channel and
the same rules as the installer and the doctor: SSH with the control plane's
key, root only through :class:`PrivilegedRunner` with a reason on every call,
and the sudo password held for one call's duration. What it does on the node
is what ``NETWORKING.md`` tells a person to do by hand — write
``/etc/netplan/40-cx7.yaml`` at 0600, ``netplan apply`` — plus the one thing
that page assumes and DGX OS does not provide: the ConnectX ports arrive under
NetworkManager's generic DHCP profiles, and a static netplan file and an
autoconnecting DHCP profile on the same port fight. Those profiles are turned
off first.

**Nothing is called done because the command returned.** After ``netplan
apply`` the address and the MTU are read back from the port, and every peer
the plan put on the same subnet is pinged over that port. A cable that does
not go where the plan assumed is a ping that fails, and the report names the
link.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

from spark_pulse.agent.bootstrap import NodeAccess, open_node_session
from spark_pulse.agent.bootstrap_probe import PrivilegedRunner, SudoDeclined, probe_node
from spark_pulse.agent.bootstrap_transport import (
    BootstrapError,
    Connector,
    NodeSession,
    Prompt,
)
from spark_pulse.agent.server import ControlPlaneServer
from spark_pulse.tools.fabric_plan import NETPLAN_PATH, NodePlan

logger = logging.getLogger(__name__)

__all__ = ["FabricApplyReport", "apply_node_plan"]

#: Where the file is staged before root moves it into place. Uploaded at
#: 0600 under the login user, then ``install``ed as root so the final file is
#: root-owned and 0600 — netplan warns about anything looser.
STAGED = ".cache/spark-pulse/40-cx7.yaml"


@dataclass
class FabricApplyReport:
    node_id: str
    name: str
    applied: bool = False
    verified: bool = False
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: netdev → what the port answered after apply.
    readback: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``(netdev, peer name, address, reachable)`` per ping.
    pings: list[dict[str, Any]] = field(default_factory=list)
    privileged_calls: list[dict[str, Any]] = field(default_factory=list)

    def note(self, step: str) -> None:
        logger.info("%s: %s", self.name, step)
        self.steps.append(step)

    def fail(self, error: str) -> None:
        logger.warning("%s: %s", self.name, error)
        self.errors.append(error)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def apply_node_plan(
    server: ControlPlaneServer,
    access: NodeAccess,
    plan: NodePlan,
    *,
    connector: Connector | None = None,
    sudo_password_prompt: Prompt | None = None,
    override_netplan: str | None = None,
) -> FabricApplyReport:
    """Write ``plan`` onto the node it is for, apply it, and read it back.

    ``override_netplan`` is an operator-supplied file that replaces the
    rendered one — the expert path, for a scheme the planner does not produce.
    It is written and applied verbatim; verification then only confirms the
    plan's ports came up with *an* address, and does not ping the plan's
    peers or require any particular MTU, because a hand-edited file may set
    both as the operator sees fit. The MTU each port ends up with is reported
    for the operator to read, never gated on.
    """
    report = FabricApplyReport(node_id=plan.node_id, name=plan.name)
    if not plan.assignments:
        report.fail("the plan has nothing to write for this node")
        return report
    try:
        session = await open_node_session(server, access, connector=connector)
    except BootstrapError as exc:
        report.fail(f"could not log in to {access.host}: {exc}")
        return report
    runner: PrivilegedRunner | None = None
    try:
        caps = await probe_node(session, username=access.username)
        runner = PrivilegedRunner(session, caps, prompt=sudo_password_prompt)
        if not runner.can("netplan apply") and sudo_password_prompt is not None:
            runner.offer_password(
                await sudo_password_prompt(
                    f"sudo password for {caps.user}@{access.host}"
                )
            )
        await _write_and_apply(
            session, runner, caps.home, plan, report, override_netplan
        )
    except SudoDeclined as exc:
        report.fail(f"needs root and none was available: {exc}")
    except BootstrapError as exc:
        report.fail(str(exc))
    finally:
        if runner is not None:
            report.privileged_calls = [asdict(call) for call in runner.calls]
            runner.drop()
        await session.close()
    return report


async def _write_and_apply(
    session: NodeSession,
    runner: PrivilegedRunner,
    home: str,
    plan: NodePlan,
    report: FabricApplyReport,
    override_netplan: str | None = None,
) -> None:
    staged = f"{home.rstrip('/')}/{STAGED}"
    made = await session.run(
        f"mkdir -p -m 0700 {shlex.quote(staged.rsplit('/', 1)[0])}", timeout=20
    )
    if not made.ok:
        raise BootstrapError(f"could not stage the file: {made.stderr.strip()[:200]}")
    content = override_netplan if override_netplan is not None else plan.netplan
    await session.upload(content.encode(), staged, mode=0o600)
    report.note(
        f"staged an operator-supplied {NETPLAN_PATH}"
        if override_netplan is not None
        else f"staged {NETPLAN_PATH} ({len(plan.assignments)} ports)"
    )

    installed = await runner.run(
        f"install -o root -g root -m 600 {shlex.quote(staged)} {shlex.quote(NETPLAN_PATH)}",
        why=f"write {NETPLAN_PATH}",
    )
    if not installed.ok:
        raise BootstrapError(
            f"could not write {NETPLAN_PATH}: {installed.stderr.strip()[:200]}"
        )
    report.note(f"wrote {NETPLAN_PATH}")

    generated = await runner.run(
        "netplan generate", why="check the netplan file parses"
    )
    if not generated.ok:
        raise BootstrapError(
            f"netplan rejected the file: {(generated.stderr or generated.stdout).strip()[:300]}"
        )
    report.note("netplan accepted the file")

    await _quiet_network_manager(session, runner, plan, report)

    applied = await runner.run("netplan apply", why="bring the fabric addresses up")
    if not applied.ok:
        raise BootstrapError(
            f"netplan apply failed: {(applied.stderr or applied.stdout).strip()[:300]}"
        )
    report.applied = True
    report.note("netplan apply ran")

    await _read_back(session, plan, report, overridden=override_netplan is not None)


async def _quiet_network_manager(
    session: NodeSession,
    runner: PrivilegedRunner,
    plan: NodePlan,
    report: FabricApplyReport,
) -> None:
    """Turn off NetworkManager's DHCP profiles on the ports the plan addresses.

    DGX OS hands every wired port a "Wired connection N" profile set to
    DHCP. Left autoconnecting, it competes with the static file on the same
    port and the address flaps or never appears. ``nmcli`` may be absent (a
    networkd machine) — then there is nothing to quiet.
    """
    listing = await session.run(
        "nmcli -t -f NAME,DEVICE connection show 2>/dev/null", timeout=20
    )
    if not listing.ok:
        report.note("NetworkManager is not managing this machine; nothing to quiet")
        return
    ours = {a.netdev for a in plan.assignments}
    for line in listing.stdout.splitlines():
        name, _, device = line.rpartition(":")
        if not name or device not in ours:
            continue
        # Our own netplan-rendered profile is named after the interface.
        if name == device or name.startswith("netplan-"):
            continue
        result = await runner.run(
            f"nmcli connection modify {shlex.quote(name)} connection.autoconnect no",
            why=f"stop the DHCP profile {name!r} from competing on {device}",
        )
        if result.ok:
            await runner.run(
                f"nmcli connection down {shlex.quote(name)}",
                why=f"take the DHCP profile {name!r} off {device}",
            )
            report.note(f"quieted NetworkManager profile {name!r} on {device}")
        else:
            report.fail(
                f"could not quiet NetworkManager profile {name!r}: {result.stderr.strip()[:200]}"
            )


async def _read_back(
    session: NodeSession,
    plan: NodePlan,
    report: FabricApplyReport,
    *,
    overridden: bool = False,
) -> None:
    """The address and MTU each port now has, and whether every peer answers.

    For an operator-supplied file the plan's cidrs, peers and MTU no longer
    describe what was written, so verification confirms only that each of the
    plan's ports came up with *some* IPv4 address; the MTU is reported but not
    gated on, and the peer pings — which would target the plan's addresses —
    are skipped.
    """
    verified = True
    for assignment in plan.assignments:
        dev = shlex.quote(assignment.netdev)
        addr = await session.run(f"ip -o -f inet addr show dev {dev}", timeout=20)
        mtu = await session.run(f"cat /sys/class/net/{dev}/mtu", timeout=20)
        if overridden:
            has_address = " inet " in addr.stdout
            found = addr.stdout.split(" inet ", 1)[1].split()[0] if has_address else ""
            report.readback[assignment.netdev] = {
                "cidr": found,
                "address_ok": has_address,
                "mtu": mtu.stdout.strip(),
                # No expected MTU for a hand-written file: reported, not gated.
                "mtu_ok": None,
            }
            if not has_address:
                verified = False
                report.fail(
                    f"{assignment.netdev} came up with no address from the "
                    "supplied file"
                )
            continue
        has_address = assignment.cidr in addr.stdout
        has_mtu = mtu.stdout.strip() == str(assignment.mtu)
        report.readback[assignment.netdev] = {
            "cidr": assignment.cidr,
            "address_ok": has_address,
            "mtu": mtu.stdout.strip(),
            "mtu_ok": has_mtu,
        }
        if not has_address:
            verified = False
            report.fail(
                f"{assignment.netdev} does not carry {assignment.cidr} after apply"
            )
        if not has_mtu:
            verified = False
            report.fail(
                f"{assignment.netdev} has MTU {mtu.stdout.strip() or '?'}, not {assignment.mtu}"
            )
        for peer_name, address in assignment.peers:
            ping = await session.run(
                f"ping -c 2 -W 2 -I {dev} {shlex.quote(address)}", timeout=20
            )
            report.pings.append(
                {
                    "netdev": assignment.netdev,
                    "peer": peer_name,
                    "address": address,
                    "reachable": ping.ok,
                }
            )
            if not ping.ok:
                verified = False
                report.fail(
                    f"{peer_name} ({address}) does not answer over {assignment.netdev}; "
                    "either that node is not applied yet, or the cable does not go where "
                    "the plan assumed"
                )
    report.verified = verified
    if verified:
        report.note("every port carries its address and every peer answers")
