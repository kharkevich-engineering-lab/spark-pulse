"""The node clean-ups: what DGX OS runs that a model server does not need.

Three properties, asserted as behaviour rather than promised in a docstring:

1. nothing is looked at that was not selected, and nothing is changed that was
   not named — a fresh install's doctor reports nothing new, and the generic
   repair never touches a clean-up;
2. every change has an inverse that puts the node back, round-tripped here on
   a simulated node;
3. the privileged commands are exact argv, so the sudoers rule is a list of
   literals and cannot be widened by a unit name.
"""

from __future__ import annotations

import pytest

from spark_pulse.agent import node_tuning
from spark_pulse.agent.bootstrap import (
    BootstrapError,
    NodeAccess,
    render_tuning_sudoers,
)
from spark_pulse.agent.doctor import FIXABLE, NEEDS_HUMAN, diagnose, treat
from spark_pulse.mock import profiles
from spark_pulse.mock.bootstrap_node import (
    SudoPolicy,
    _split_operators,
    host_probe_handler,
    spark_like_node,
)
from tests.agent_bootstrap_fixtures import (
    PASSWORD,
    USER,
    do_install,
    make_node,
    password_prompt,
)

ALL = list(node_tuning.TUNING_IDS)


def access() -> NodeAccess:
    return NodeAccess(host="10.0.0.7", username=USER)


# ── The catalogue, pure ─────────────────────────────────────────────────────


def test_the_catalogue_is_the_eight_items_in_a_fixed_order():
    assert ALL == [
        "desktop",
        "bluetooth",
        "printing",
        "snapd",
        "dgx_dashboard",
        "fwupd_refresh",
        "watermark_boost",
        "splash",
    ]
    assert node_tuning.check_name("dgx_dashboard") == "tuning-dgx-dashboard"
    assert node_tuning.tuning_for("avahi") is None


def test_the_desktop_and_snapd_are_the_ones_that_ask_for_a_confirm():
    flagged = [t.id for t in node_tuning.TUNINGS if t.consequential]
    assert flagged == ["desktop", "snapd"]


@pytest.mark.parametrize(
    "tuning", [t for t in node_tuning.TUNINGS if not t.advisory], ids=lambda t: t.id
)
def test_every_item_can_be_undone(tuning):
    applied = node_tuning.apply_commands(tuning)
    restored = node_tuning.restore_commands(tuning)
    assert applied and restored
    if tuning.units:
        for unit in tuning.units:
            assert f"{node_tuning.SYSTEMCTL} disable --now {unit}" in applied
            assert f"{node_tuning.SYSTEMCTL} enable --now {unit}" in restored


def test_the_desktop_is_set_to_text_and_back():
    desktop = node_tuning.tuning_for("desktop")
    assert node_tuning.apply_commands(desktop) == [
        "/usr/bin/systemctl set-default multi-user.target",
        "/usr/bin/systemctl disable --now gdm3.service",
    ]
    assert node_tuning.restore_commands(desktop) == [
        "/usr/bin/systemctl set-default graphical.target",
        "/usr/bin/systemctl enable --now gdm3.service",
    ]


def test_fwupd_stays_installed_and_only_its_refresh_timer_goes():
    fwupd = node_tuning.tuning_for("fwupd_refresh")
    assert node_tuning.apply_commands(fwupd) == [
        "/usr/bin/systemctl disable --now fwupd-refresh.timer",
        "/usr/bin/systemctl stop fwupd.service",
    ]
    restored = node_tuning.restore_commands(fwupd)
    assert restored == ["/usr/bin/systemctl enable --now fwupd-refresh.timer"]


def test_a_unit_the_node_lacks_is_left_out_of_the_commands():
    dashboard = node_tuning.tuning_for("dgx_dashboard")
    assert node_tuning.apply_commands(dashboard, ("dgx-dashboard.service",)) == [
        "/usr/bin/systemctl disable --now dgx-dashboard.service"
    ]


def test_the_watermark_boost_goes_back_to_the_kernels_default():
    boost = node_tuning.tuning_for("watermark_boost")
    assert node_tuning.apply_commands(boost) == [
        "/usr/bin/tee /etc/sysctl.d/90-watermark-boost.conf",
        "/usr/sbin/sysctl -w vm.watermark_boost_factor=0",
    ]
    assert node_tuning.restore_commands(boost) == [
        "/usr/bin/rm -f /etc/sysctl.d/90-watermark-boost.conf",
        "/usr/sbin/sysctl -w vm.watermark_boost_factor=15000",
    ]


def test_commands_read_the_way_an_operator_would_type_them():
    lines = node_tuning.operator_lines(
        node_tuning.apply_commands(node_tuning.tuning_for("watermark_boost"))
    )
    assert lines == [
        "echo 'vm.watermark_boost_factor = 0' | sudo tee "
        "/etc/sysctl.d/90-watermark-boost.conf",
        "sudo sysctl -w vm.watermark_boost_factor=0",
    ]


def test_systemctl_show_is_parsed_per_unit():
    text = (
        "Id=a.service\nLoadState=loaded\nActiveState=active\n"
        "UnitFileState=enabled\nMemoryCurrent=1048576\n\n"
        "Id=b.service\nLoadState=not-found\nActiveState=inactive\n"
        "UnitFileState=\nMemoryCurrent=[not set]\n\n"
        "Id=c.service\nLoadState=loaded\nActiveState=active\n"
        "UnitFileState=static\nMemoryCurrent=18446744073709551615\n"
    )
    found = node_tuning.parse_show(text)
    assert found["a.service"].memory_bytes == 1048576
    assert found["a.service"].on and found["a.service"].present
    assert not found["b.service"].present and not found["b.service"].on
    assert found["c.service"].memory_bytes is None  # the "no cgroup" sentinel


def test_a_masked_unit_is_off():
    masked = node_tuning.UnitReading("x", "masked", "masked", "inactive")
    assert masked.present and not masked.on


def test_snap_names_come_from_the_list_without_its_header():
    text = "Name  Version  Rev\nfirefox  1  2\ngnome-42  3  4\n"
    assert node_tuning.parse_snap_list(text) == ["firefox", "gnome-42"]
    assert node_tuning.parse_snap_list("") == []


# ── The sudoers rule ────────────────────────────────────────────────────────


def test_the_sudoers_rule_is_one_literal_command_per_line_and_no_wildcard():
    rule = render_tuning_sudoers("spark")
    grants = [line for line in rule.splitlines() if not line.startswith("#")]
    assert grants
    assert all(line.startswith("spark ALL=(root) NOPASSWD: /usr/") for line in grants)
    assert "*" not in "".join(grants)
    for line in (
        "spark ALL=(root) NOPASSWD: /usr/bin/systemctl disable --now bluetooth.service",
        "spark ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now bluetooth.service",
        "spark ALL=(root) NOPASSWD: /usr/bin/systemctl set-default multi-user.target",
        "spark ALL=(root) NOPASSWD: /usr/bin/systemctl set-default graphical.target",
        "spark ALL=(root) NOPASSWD: /usr/bin/tee /etc/sysctl.d/90-watermark-boost.conf",
        "spark ALL=(root) NOPASSWD: /usr/sbin/sysctl -w vm.watermark_boost_factor=0",
        "spark ALL=(root) NOPASSWD: /usr/sbin/sysctl -w vm.watermark_boost_factor=15000",
        "spark ALL=(root) NOPASSWD: /usr/bin/rm -f /etc/sysctl.d/90-watermark-boost.conf",
    ):
        assert line in grants


def test_every_command_the_doctor_can_run_is_in_the_rule():
    allowed = set(node_tuning.sudoers_commands())
    for tuning in node_tuning.TUNINGS:
        for command in (
            *node_tuning.apply_commands(tuning),
            *node_tuning.restore_commands(tuning),
        ):
            assert command in allowed
    # And nothing a unit name could widen: no bare verb with a free argument.
    assert not any(c.endswith(("systemctl", "disable", "enable")) for c in allowed)


def test_the_sudoers_rule_refuses_a_hostile_username():
    for hostile in ("alex\nroot ALL=(ALL) NOPASSWD: ALL", "a b", "%wheel", "a,b"):
        with pytest.raises(BootstrapError):
            render_tuning_sudoers(hostile)


async def test_an_install_that_offers_sudoers_writes_and_validates_the_rule(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    report = await do_install(
        agent_server, agent_fleet, node, agent_bundle, offer_sudoers=True
    )

    written = node.read("/etc/sudoers.d/spark-pulse-agent-tuning").decode()
    assert written == render_tuning_sudoers(USER)
    assert not any(c.capability == "tuning-sudoers" for c in report.concessions)
    # Staged and parsed before it was installed.
    assert any(
        "visudo -cf" in e["command"] and "sudoers-tuning" in e["command"]
        for e in node.commands
    )


async def test_an_install_that_does_not_offer_sudoers_grants_nothing(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node = make_node(tmp_path)
    await do_install(agent_server, agent_fleet, node, agent_bundle)
    assert not node.exists("/etc/sudoers.d/spark-pulse-agent-tuning")


# ── The doctor over a simulated DGX OS node ─────────────────────────────────

READ_ONLY_VERBS = {"id", "printf", "hostname", "uname", "docker", "journalctl"}


def changed(node, since: int) -> list[str]:
    """Commands since ``since`` that could have changed the node."""
    found = []
    for entry in node.commands[since:]:
        for _op, clause in _split_operators(entry["command"]):
            parts = clause.split()
            if parts[:1] == ["sudo"]:
                parts = [p for p in parts[1:] if not p.startswith("-")]
            if not parts:
                continue
            verb = parts[0].rsplit("/", 1)[-1]
            if verb in ("tee", "sysctl", "rm"):
                found.append(clause.strip())
            if verb == "systemctl":
                rest = [p for p in parts[1:] if not p.startswith("-")]
                if rest and rest[0] in ("disable", "enable", "stop", "set-default"):
                    found.append(clause.strip())
    return found


async def enrolled(agent_server, agent_fleet, agent_bundle, tmp_path, **kwargs):
    kwargs.setdefault("profile", profiles.SPARK)
    node = make_node(tmp_path, **kwargs)
    report = await do_install(agent_server, agent_fleet, node, agent_bundle)
    return node, report.node_id


async def test_a_fresh_install_reports_no_clean_up_at_all(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    mark = len(node.commands)

    report = await diagnose(
        agent_server, node_id, access=access(), connector=agent_fleet
    )

    assert not [f for f in report.findings if f.check.startswith("tuning-")]
    assert not any("snap" in e["command"] for e in node.commands[mark:])


async def test_selected_clean_ups_are_read_and_nothing_is_changed(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    mark = len(node.commands)

    report = await diagnose(
        agent_server, node_id, access=access(), connector=agent_fleet, tunings=ALL
    )

    found = {f.check: f for f in report.findings if f.check.startswith("tuning-")}
    assert sorted(found) == sorted(node_tuning.check_name(i) for i in ALL)
    for finding in found.values():
        assert finding.status == "warn"
        assert finding.verdict == (
            NEEDS_HUMAN if finding.tuning["id"] == "splash" else FIXABLE
        )
        assert finding.channel == "ssh"
        assert finding.tuning["present"] and not finding.tuning["applied"]
        assert "sudo " in finding.remedy and finding.remedy.startswith(
            ("run: ", "do: ")
        )
    assert changed(node, mark) == []
    assert node.default_target == "graphical.target"
    # Costing memory is not a fault, so the node still reads healthy.
    assert report.healthy and report.problems == []


async def test_a_finding_says_what_it_holds_and_what_it_costs(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)

    report = await diagnose(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        tunings=["desktop", "snapd", "watermark_boost"],
    )

    desktop = report.get("tuning-desktop")
    assert desktop.tuning["default_target"] == "graphical.target"
    assert desktop.tuning["memory_bytes"] == 640 * 1024 * 1024
    assert "640 MiB" in desktop.detail
    assert "text console" in desktop.detail
    snapd = report.get("tuning-snapd")
    assert snapd.tuning["snaps"] == [
        "firefox",
        "snapd-desktop-integration",
        "gnome-42-2204",
        "bare",
        "core22",
    ]
    assert "firefox" in snapd.detail
    assert report.get("tuning-watermark-boost").tuning["value"] == 15000


async def test_a_unit_the_node_does_not_have_is_not_a_finding(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    # The lab workstation is an Ubuntu desktop: no dgx-dashboard.
    node, node_id = await enrolled(
        agent_server, agent_fleet, agent_bundle, tmp_path, profile=profiles.WORKSTATION
    )

    report = await diagnose(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        tunings=["dgx_dashboard", "bluetooth"],
    )

    dashboard = report.get("tuning-dgx-dashboard")
    assert dashboard.status == "ok"
    assert dashboard.detail == "not present on this node"
    assert dashboard.tuning["present"] is False
    assert report.get("tuning-bluetooth").status == "warn"


async def test_a_bare_server_has_none_of_it(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(
        agent_server, agent_fleet, agent_bundle, tmp_path, profile=None
    )

    report = await diagnose(
        agent_server, node_id, access=access(), connector=agent_fleet, tunings=ALL
    )

    desktop = report.get("tuning-desktop")
    assert desktop.status == "ok" and desktop.tuning["present"] is False
    assert report.healthy


async def test_apply_changes_the_named_item_re_reads_it_and_restore_undoes_it(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    mark = len(node.commands)

    treated = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        tunings=ALL,
        apply=["desktop", "bluetooth"],
    )

    repairs = {r.check: r for r in treated.repairs}
    assert repairs["tuning-desktop"].applied, repairs["tuning-desktop"].detail
    assert repairs["tuning-bluetooth"].applied
    assert node.default_target == "multi-user.target"
    assert not node.system_units["gdm3.service"].active
    assert not node.system_units["gdm3.service"].enabled
    assert not node.system_units["bluetooth.service"].active
    # Re-verified from the node, and only those two moved.
    assert treated.get("tuning-desktop").tuning["applied"] is True
    assert treated.get("tuning-bluetooth").tuning["applied"] is True
    assert treated.get("tuning-printing").tuning["applied"] is False
    assert node.system_units["cups.service"].active
    assert node.system_units["snapd.service"].active
    assert all(
        "snapd" not in c and "cups" not in c for c in changed(node, mark)
    ), "an unnamed item was touched"

    restored = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        tunings=ALL,
        restore=["desktop", "bluetooth"],
    )

    assert all(r.applied for r in restored.repairs), restored.repairs
    assert node.default_target == "graphical.target"
    assert node.system_units["gdm3.service"].active
    assert node.system_units["gdm3.service"].enabled
    assert node.system_units["bluetooth.service"].enabled
    assert restored.get("tuning-desktop").tuning["applied"] is False


@pytest.mark.parametrize(
    "item", ["printing", "snapd", "dgx_dashboard", "fwupd_refresh"]
)
async def test_each_unit_group_round_trips(
    agent_server, agent_fleet, agent_bundle, tmp_path, item
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)

    # fwupd.service itself is stopped by applying and comes back on demand
    # (D-Bus activation), so it is not part of what restore puts back.
    def state():
        return {
            n: (u.enabled, u.active)
            for n, u in node.system_units.items()
            if n != "fwupd.service"
        }

    before = state()
    units = node_tuning.tuning_for(item).units

    async def run(**kwargs):
        return await treat(
            agent_server,
            node_id,
            access=access(),
            connector=agent_fleet,
            sudo_password_prompt=password_prompt(PASSWORD),
            tunings=[item],
            **kwargs,
        )

    applied = await run(apply=[item])
    assert applied.repairs[0].applied, applied.repairs[0].detail
    for unit in units:
        assert not node.system_units[unit].enabled
        assert not node.system_units[unit].active
    assert applied.get(node_tuning.check_name(item)).tuning["applied"] is True

    restored = await run(restore=[item])
    assert restored.repairs[0].applied, restored.repairs[0].detail
    assert state() == before


async def test_fwupd_itself_is_stopped_but_stays_enabled_and_installed(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)

    await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        tunings=["fwupd_refresh"],
        apply=["fwupd_refresh"],
    )

    assert not node.system_units["fwupd-refresh.timer"].enabled
    assert not node.system_units["fwupd.service"].active
    assert node.system_units["fwupd.service"].enabled


async def test_the_watermark_boost_is_written_through_tee_and_restored_to_15000(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    knob = "/proc/sys/vm/watermark_boost_factor"
    conf = "/etc/sysctl.d/90-watermark-boost.conf"

    applied = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        tunings=["watermark_boost"],
        apply=["watermark_boost"],
    )

    assert applied.repairs[0].applied, applied.repairs[0].detail
    assert node.read(conf).decode() == "vm.watermark_boost_factor = 0\n"
    assert node.read(knob).decode().strip() == "0"
    assert applied.get("tuning-watermark-boost").tuning["applied"] is True
    # The file's content travelled on stdin, never in the command line.
    tee = [e for e in node.commands if "tee /etc/sysctl.d" in e["command"]]
    assert tee and "vm.watermark_boost_factor" not in tee[-1]["command"]

    restored = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        tunings=["watermark_boost"],
        restore=["watermark_boost"],
    )

    assert restored.repairs[0].applied, restored.repairs[0].detail
    assert not node.exists(conf)
    assert node.read(knob).decode().strip() == "15000"


async def test_the_generic_repair_never_applies_a_clean_up(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    mark = len(node.commands)

    treated = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        tunings=ALL,
    )

    assert changed(node, mark) == []
    assert node.default_target == "graphical.target"
    assert treated.repairs == []
    assert treated.get("tuning-desktop").tuning["applied"] is False


async def test_applying_an_applied_item_or_restoring_a_plain_one_does_nothing(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    mark = len(node.commands)

    treated = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        tunings=ALL,
        restore=["bluetooth"],
    )

    assert not treated.repairs[0].applied
    assert "nothing to undo" in treated.repairs[0].detail
    assert changed(node, mark) == []


async def test_a_clean_up_that_was_never_selected_can_still_be_named(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """Naming is the act. Settings only chooses what the doctor *shows*."""
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)

    treated = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        apply=["bluetooth"],
    )

    assert treated.repairs[0].applied
    assert not node.system_units["bluetooth.service"].active


async def test_without_a_sudo_path_a_clean_up_is_a_line_to_run_there(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(
        agent_server,
        agent_fleet,
        agent_bundle,
        tmp_path,
        sudo=SudoPolicy(mode="none"),
    )
    mark = len(node.commands)

    found = await diagnose(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        tunings=["desktop"],
    )
    desktop = found.get("tuning-desktop")
    assert desktop.verdict == NEEDS_HUMAN
    assert desktop.remedy == (
        "run: sudo systemctl set-default multi-user.target "
        "&& sudo systemctl disable --now gdm3.service"
    )

    treated = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        tunings=["desktop"],
        apply=["desktop"],
    )
    assert not treated.repairs[0].applied
    assert NEEDS_HUMAN in treated.repairs[0].detail
    assert changed(node, mark) == []
    assert node.default_target == "graphical.target"


async def test_a_wrong_sudo_password_leaves_the_node_as_it_was(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)

    treated = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt("not-the-password"),
        apply=["desktop"],
    )

    assert not treated.repairs[0].applied
    assert node.default_target == "graphical.target"
    assert node.system_units["gdm3.service"].active


async def test_the_rule_the_installer_writes_makes_every_command_passwordless(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    """What ``render_tuning_sudoers`` grants is what ``free_for`` matches."""
    node, node_id = await enrolled(
        agent_server,
        agent_fleet,
        agent_bundle,
        tmp_path,
        sudo=SudoPolicy(mode="scoped", commands=tuple(node_tuning.sudoers_commands())),
    )
    prompts = node.sudo_prompts

    treated = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        tunings=ALL,
        apply=ALL,
    )

    by_check = {r.check: r for r in treated.repairs}
    assert all(
        by_check[node_tuning.check_name(i)].applied for i in ALL if i != "splash"
    ), [(r.check, r.detail) for r in treated.repairs]
    assert not by_check["tuning-splash"].applied
    assert node.sudo_prompts == prompts, "a password was asked for"
    assert all(
        treated.get(node_tuning.check_name(i)).tuning["applied"]
        for i in ALL
        if i != "splash"
    )


async def test_a_node_whose_systemctl_does_not_answer_is_unknown_not_clean(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    node.system_manager = False

    report = await diagnose(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        tunings=["bluetooth"],
    )

    assert report.get("tuning-bluetooth").status == "unknown"


async def test_the_control_node_is_read_over_its_own_agent_and_given_the_commands(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    stub = node.agent_runner.agent_for(node.host)
    stub.handler = host_probe_handler(node, USER)

    found = await diagnose(agent_server, node_id, tunings=ALL)

    for tuning_id in ALL:
        finding = found.get(node_tuning.check_name(tuning_id))
        assert finding.channel == "agent"
        assert finding.status == "warn"
        assert finding.verdict == NEEDS_HUMAN
        assert "sudo " in finding.remedy and finding.remedy.startswith(
            ("run: ", "do: ")
        )
        assert finding.tuning["can_treat"] is False
        assert finding.tuning["restore_commands"]
    assert found.get("tuning-snapd").tuning["snaps"][0] == "firefox"
    assert found.get("tuning-watermark-boost").tuning["value"] == 15000


async def test_an_agent_that_cannot_run_the_probe_leaves_each_item_unknown(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)

    found = await diagnose(agent_server, node_id, tunings=["desktop", "snapd"])

    assert found.get("tuning-desktop").status == "unknown"
    assert found.get("tuning-snapd").status == "unknown"
    assert found.healthy


async def test_an_unreachable_node_leaves_each_item_unknown(agent_server):
    found = await diagnose(agent_server, "no-such-node", tunings=["bluetooth"])

    assert found.get("tuning-bluetooth").status == "unknown"


def test_spark_like_node_takes_a_profile(tmp_path):
    node = spark_like_node("10.0.0.9", tmp_path, profile=profiles.SPARK)
    assert node.default_target == "graphical.target"
    assert "dgx-dashboard.service" in node.system_units


def test_the_splash_item_has_no_commands_and_no_sudoers_line():
    splash = node_tuning.tuning_for("splash")
    assert splash.advisory
    assert node_tuning.apply_commands(splash) == []
    assert node_tuning.restore_commands(splash) == []
    assert not any("grub" in c for c in node_tuning.sudoers_commands())
    assert node_tuning.advisory_lines(splash, "apply") == [
        "edit GRUB_CMDLINE_LINUX_DEFAULT in /etc/default/grub and remove splash",
        "sudo update-grub",
        "sudo reboot",
    ]
    assert "add splash back" in node_tuning.advisory_lines(splash, "restore")[0]


async def test_splash_is_advice_only_over_ssh_and_is_never_treated(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    mark = len(node.commands)

    found = await diagnose(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        tunings=["splash"],
    )
    splash = found.get("tuning-splash")
    assert splash.status == "warn"
    assert splash.verdict == NEEDS_HUMAN
    assert splash.tuning["cmdline_has_splash"] is True
    assert splash.tuning["can_treat"] is False
    assert "update-grub" in splash.remedy and "reboot" in splash.remedy
    assert "add splash back" in splash.remedy
    assert found.healthy

    treated = await treat(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        sudo_password_prompt=password_prompt(PASSWORD),
        tunings=["splash"],
        apply=["splash"],
    )
    assert not treated.repairs[0].applied
    assert NEEDS_HUMAN in treated.repairs[0].detail
    assert changed(node, mark) == []


async def test_a_command_line_without_splash_reads_as_applied(
    agent_server, agent_fleet, agent_bundle, tmp_path
):
    node, node_id = await enrolled(agent_server, agent_fleet, agent_bundle, tmp_path)
    cmdline = node.path("/proc/cmdline", "root")
    cmdline.write_text("BOOT_IMAGE=/boot/vmlinuz ro quiet\n")

    found = await diagnose(
        agent_server,
        node_id,
        access=access(),
        connector=agent_fleet,
        tunings=["splash"],
    )

    assert found.get("tuning-splash").status == "ok"
    assert found.get("tuning-splash").tuning["applied"] is True
