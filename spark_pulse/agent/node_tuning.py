"""What a DGX OS node runs that a model server does not need, and how to stop it.

On unified memory every MiB a background service holds is a MiB the model
cannot have. DGX OS ships a full desktop, a print server, a package daemon, a
dashboard whose update check peaks near 850 MiB, and a kernel setting that can
hide up to 0.87 GiB of ``MemAvailable``. This module is the catalogue of those,
in one place and pure: the doctor reads a node against it, the settings router
validates the operator's selection against it, and the sudoers drop-in is
rendered from the same command lists, so the three cannot disagree about what a
tuning *is*.

Three rules that hold the design:

* **Nothing is applied unasked.** The doctor looks at the tunings the operator
  selected in Settings and nothing else, and it changes a node only when a
  request names a tuning. Selecting one is "check this", never "do this".
* **Every tuning can be undone.** :func:`restore_commands` is the exact inverse
  of :func:`apply_commands`. The one value that is not recorded — the kernel's
  watermark boost — goes back to :data:`WATERMARK_BOOST_DEFAULT`, the kernel's
  own default, because the node's earlier value is not kept anywhere.
* **A unit that is not on the node is "not present", never a finding.** The
  catalogue lists what DGX OS ships; another machine ships less.

Left alone on purpose: ``avahi-daemon``, ``ModemManager``, ``wpa_supplicant``,
``udisks2``, ``upower`` and ``multipathd``. They hold 2 to 25 MiB each, several
are how a node is found or reached (mDNS, Wi-Fi), and the saving is not worth
the chance of a node that cannot be reached afterwards.

The kernel command line (removing ``splash``) is not here: it is a GRUB edit
and a reboot, which is a human on that machine. ``docs/guide/monitoring.md``
says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "SYSTEMCTL",
    "SYSCTL",
    "TEE",
    "RM",
    "TUNINGS",
    "TUNING_IDS",
    "WATERMARK_BOOST_FILE",
    "WATERMARK_BOOST_PATH",
    "WATERMARK_BOOST_DEFAULT",
    "SNAP_NAMES_COMMAND",
    "Tuning",
    "UnitReading",
    "check_name",
    "tuning_for",
    "probe_commands",
    "parse_show",
    "parse_snap_list",
    "apply_commands",
    "restore_commands",
    "sudoers_commands",
    "operator_lines",
    "advisory_lines",
    "CMDLINE_PATH",
    "WATERMARK_BOOST_CONF",
]

#: Absolute paths, because sudoers matches the command's path and the doctor's
#: sudo check compares the command string whole. These are the merged-/usr
#: locations of Ubuntu 24.04, which is what DGX OS is.
SYSTEMCTL = "/usr/bin/systemctl"
SYSCTL = "/usr/sbin/sysctl"
TEE = "/usr/bin/tee"
RM = "/usr/bin/rm"

WATERMARK_BOOST_PATH = "/proc/sys/vm/watermark_boost_factor"
CMDLINE_PATH = "/proc/cmdline"
WATERMARK_BOOST_FILE = "/etc/sysctl.d/90-watermark-boost.conf"
#: The kernel's default, and what restore writes back: the node's own earlier
#: value is not recorded anywhere, so the one the kernel ships stands in for it.
WATERMARK_BOOST_DEFAULT = 15000

SNAP_NAMES_COMMAND = "snap list 2>/dev/null"

_DESKTOP_UNIT = "gdm3.service"
_MULTI_USER = "multi-user.target"
_GRAPHICAL = "graphical.target"


@dataclass(frozen=True)
class Tuning:
    """One thing the doctor can look at and, when asked, change."""

    id: str
    #: The units this disables, in the order they are stopped. Empty for the
    #: sysctl.
    units: tuple[str, ...] = ()
    #: Units stopped (not disabled) as part of applying, when they are there.
    stop_units: tuple[str, ...] = ()
    #: What it frees, what it costs, and how it is undone: one line each, for
    #: the finding's detail. The page words these in the operator's language.
    frees: str = ""
    loses: str = ""
    undo: str = ""
    #: Whether applying it changes what a person at the machine sees or loses.
    #: The UI puts these behind a confirm that says so.
    consequential: bool = False
    #: Read and reported, never changed from here. The kernel command line is a
    #: GRUB edit and a reboot, which is a person on that machine.
    advisory: bool = False


TUNINGS: tuple[Tuning, ...] = (
    Tuning(
        id="desktop",
        units=(_DESKTOP_UNIT,),
        frees="the GNOME session, about 1 GiB with a user logged in",
        loses="a monitor on the node shows a text console, not a desktop",
        undo="systemctl set-default graphical.target, then enable --now gdm3",
        consequential=True,
    ),
    Tuning(
        id="bluetooth",
        units=("bluetooth.service",),
        frees="the Bluetooth daemon",
        loses="Bluetooth keyboards and audio",
        undo="systemctl enable --now bluetooth.service",
    ),
    Tuning(
        id="printing",
        units=(
            "cups.socket",
            "cups.path",
            "cups-browsed.service",
            "cups.service",
        ),
        frees="the print server and its network printer discovery",
        loses="printing from this node",
        undo="systemctl enable --now the cups units",
    ),
    Tuning(
        id="snapd",
        units=(
            "snapd.socket",
            "snapd.seeded.service",
            "snapd.snap-repair.timer",
            "snapd.service",
        ),
        frees="the snap daemon and its refresh timers",
        loses="every installed snap stops updating, and any snap-run service stops",
        undo="systemctl enable --now the snapd units",
        consequential=True,
    ),
    Tuning(
        id="dgx_dashboard",
        units=("dgx-dashboard.service", "dgx-dashboard-admin.service"),
        frees="the dashboard, whose update check peaks near 850 MiB and wakes "
        "fwupd and PackageKit",
        loses="the DGX dashboard web page",
        undo="systemctl enable --now the dgx-dashboard units",
    ),
    Tuning(
        id="fwupd_refresh",
        units=("fwupd-refresh.timer",),
        stop_units=("fwupd.service",),
        frees="the daily firmware metadata refresh; fwupd stays installed",
        loses="automatic firmware update checks; run fwupdmgr by hand",
        undo="systemctl enable --now fwupd-refresh.timer",
    ),
    Tuning(
        id="watermark_boost",
        frees="up to 0.87 GiB of MemAvailable the kernel holds back after "
        "fragmentation",
        loses="nothing measurable on a node that serves models",
        undo=f"remove {WATERMARK_BOOST_FILE} and set the value back to "
        f"{WATERMARK_BOOST_DEFAULT}",
    ),
    Tuning(
        id="splash",
        frees="the boot splash, a little memory held until the desktop starts",
        loses="the graphical boot screen",
        undo="add splash back to GRUB_CMDLINE_LINUX_DEFAULT, update-grub, reboot",
        advisory=True,
    ),
)

TUNING_IDS: tuple[str, ...] = tuple(t.id for t in TUNINGS)
_BY_ID = {t.id: t for t in TUNINGS}


def tuning_for(tuning_id: str) -> Tuning | None:
    return _BY_ID.get(tuning_id)


def check_name(tuning_id: str) -> str:
    """The doctor finding's ``check`` for a tuning. ``dgx_dashboard`` reads as
    ``tuning-dgx-dashboard``, like every other check name."""
    return "tuning-" + tuning_id.replace("_", "-")


# ── Reading a node ──────────────────────────────────────────────────────────

_PROPERTIES = "-p Id -p LoadState -p UnitFileState -p ActiveState -p MemoryCurrent"


def probe_commands(tuning: Tuning) -> list[str]:
    """The read-only commands that answer for one tuning, unprivileged.

    ``systemctl show`` answers for a unit that does not exist — ``LoadState``
    says ``not-found`` and the exit status is 0 — which is what makes "not
    present" a reading and not an error.
    """
    if tuning.id == "watermark_boost":
        return [f"cat {WATERMARK_BOOST_PATH} 2>/dev/null"]
    if tuning.id == "splash":
        return [f"cat {CMDLINE_PATH} 2>/dev/null"]
    units = " ".join((*tuning.units, *tuning.stop_units))
    commands = [f"systemctl show {_PROPERTIES} {units} 2>/dev/null"]
    if tuning.id == "desktop":
        commands.insert(0, "systemctl get-default 2>/dev/null")
    return commands


@dataclass(frozen=True)
class UnitReading:
    name: str
    load_state: str = ""
    file_state: str = ""
    active_state: str = ""
    memory_bytes: int | None = None

    @property
    def present(self) -> bool:
        return self.load_state not in ("", "not-found")

    @property
    def on(self) -> bool:
        """Running, or set to run at boot. A masked unit is neither."""
        if self.load_state == "masked":
            return False
        return self.active_state in ("active", "activating", "reloading") or (
            self.file_state in ("enabled", "enabled-runtime", "alias")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.file_state in ("enabled", "enabled-runtime"),
            "active": self.active_state in ("active", "activating", "reloading"),
            "memory_bytes": self.memory_bytes,
        }


#: ``MemoryCurrent`` is ``[not set]`` when accounting is off and 2**64-1 when a
#: unit has no cgroup yet; neither is a number of bytes.
_UNSET_MEMORY = 2**63


def parse_show(text: str) -> dict[str, UnitReading]:
    """``systemctl show`` of several units: stanzas of ``key=value``, blank-separated."""
    readings: dict[str, UnitReading] = {}
    for stanza in re.split(r"\n\s*\n", text.strip()):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        name = fields.get("Id")
        if not name:
            continue
        memory: int | None = None
        raw = fields.get("MemoryCurrent", "")
        if raw.isdigit() and int(raw) < _UNSET_MEMORY:
            memory = int(raw)
        readings[name] = UnitReading(
            name=name,
            load_state=fields.get("LoadState", ""),
            file_state=fields.get("UnitFileState", ""),
            active_state=fields.get("ActiveState", ""),
            memory_bytes=memory,
        )
    return readings


def parse_snap_list(text: str) -> list[str]:
    """Snap names from ``snap list``, header dropped, in the order shown."""
    names: list[str] = []
    for line in text.splitlines()[1:]:
        token = line.split()[:1]
        if token:
            names.append(token[0])
    return names


# ── Changing a node ─────────────────────────────────────────────────────────


def _unit_commands(verb: str, units: tuple[str, ...]) -> list[str]:
    return [f"{SYSTEMCTL} {verb} {unit}" for unit in units]


def apply_commands(tuning: Tuning, present: tuple[str, ...] | None = None) -> list[str]:
    """The privileged commands that apply a tuning, one argv each.

    One command per unit, never ``disable --now a b c``: the sudoers rule that
    allows them is an exact argv per unit name, and a combined line would match
    none of them. ``present`` limits the units to those the node has —
    ``disable`` on a unit that does not exist fails — and ``None`` means all.
    """
    if tuning.advisory:
        return []
    if tuning.id == "watermark_boost":
        return [
            f"{TEE} {WATERMARK_BOOST_FILE}",
            f"{SYSCTL} -w vm.watermark_boost_factor=0",
        ]
    units = tuple(u for u in tuning.units if present is None or u in present)
    commands: list[str] = []
    if tuning.id == "desktop":
        commands.append(f"{SYSTEMCTL} set-default {_MULTI_USER}")
    commands += _unit_commands("disable --now", units)
    commands += _unit_commands(
        "stop", tuple(u for u in tuning.stop_units if present is None or u in present)
    )
    return commands


def restore_commands(
    tuning: Tuning, present: tuple[str, ...] | None = None
) -> list[str]:
    """The inverse of :func:`apply_commands`, in the order that brings it back."""
    if tuning.advisory:
        return []
    if tuning.id == "watermark_boost":
        return [
            f"{RM} -f {WATERMARK_BOOST_FILE}",
            f"{SYSCTL} -w vm.watermark_boost_factor={WATERMARK_BOOST_DEFAULT}",
        ]
    units = tuple(u for u in tuning.units if present is None or u in present)
    commands: list[str] = []
    if tuning.id == "desktop":
        commands.append(f"{SYSTEMCTL} set-default {_GRAPHICAL}")
    # The service before its socket and path: the reverse of how it came down.
    commands += _unit_commands("enable --now", tuple(reversed(units)))
    return commands


#: What :func:`apply_commands` feeds ``tee`` on stdin.
WATERMARK_BOOST_CONF = "vm.watermark_boost_factor = 0\n"


def sudoers_commands() -> list[str]:
    """Every command either direction can run, exactly, for the sudoers rule.

    Derived from the same functions the doctor runs, with every unit assumed
    present, so a unit added to the catalogue is in the rule by construction.
    """
    seen: dict[str, None] = {}
    for tuning in TUNINGS:
        for command in (*apply_commands(tuning), *restore_commands(tuning)):
            seen[command] = None
    return list(seen)


def advisory_lines(tuning: Tuning, direction: str) -> list[str]:
    """What an operator does by hand for an advisory item, in order.

    Not commands the doctor runs, so none of this is in the sudoers rule.
    """
    edit = "edit GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub and " + (
        "remove splash" if direction == "apply" else "add splash back"
    )
    return [edit, "sudo update-grub", "sudo reboot"]


def operator_lines(commands: list[str]) -> list[str]:
    """The same commands as a person would type them: ``sudo`` first, the
    absolute path dropped, and ``tee`` given the line it is fed on stdin."""
    lines: list[str] = []
    for command in commands:
        if command.startswith(f"{TEE} "):
            target = command.split(" ", 1)[1]
            body = WATERMARK_BOOST_CONF.strip()
            lines.append(f"echo '{body}' | sudo tee {target}")
            continue
        argv0, _, rest = command.partition(" ")
        lines.append(f"sudo {argv0.rsplit('/', 1)[-1]} {rest}".rstrip())
    return lines
