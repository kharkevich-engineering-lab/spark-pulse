"""The node registry: minted identity, durable state, and honest diagnostics.

These exercise the *real* module (``import spark_pulse.tools.node_registry``,
not ``from spark_pulse.tools import ...``, which under SIMULATION_MODE=1 would
hand back the mock). The registry path is redirected at the module level so
nothing here touches a developer's ``~/.config/spark-pulse``.
"""

from __future__ import annotations

import json
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlalchemy import event

import spark_pulse.tools  # noqa: F401 — ensures the package is loaded
import spark_pulse.tools.node_registry  # noqa: F401 — the real submodule
from spark_pulse.tools.atomic_json import StateFileError
from spark_pulse.tools.discovery import NetworkInterface

# ``spark_pulse.tools.node_registry`` as an *attribute* is the mock under
# SIMULATION_MODE=1, which pytest-env forces. The real module is the one in
# ``sys.modules``, and it is the one these tests are about.
node_registry = sys.modules["spark_pulse.tools.node_registry"]
tools_package = sys.modules["spark_pulse.tools"]


def fake_discovery(**overrides):
    """A stand-in for whichever discovery module ``diagnose`` will resolve to.

    ``diagnose`` does ``from spark_pulse.tools import discovery``, so the thing
    to replace is the attribute on the package — the same switch production
    uses to pick real or mock.
    """
    from spark_pulse.tools.discovery import FabricConfig

    module = types.SimpleNamespace(
        mdns_hostname_history=dict,
        mdns_available=lambda: True,
        detect_network_interfaces=list,
        detect_link_local_addresses=dict,
        detect_local_ip=lambda: None,
        # No ConnectX by default, which is every machine that is not a Spark.
        detect_fabric=FabricConfig,
    )
    for name, value in overrides.items():
        setattr(module, name, value)
    return module


@pytest.fixture(autouse=True)
def registry_in_tmp(tmp_path, monkeypatch):
    """Point the registry at a temp file for every test in this module."""
    path = tmp_path / "nodes.json"
    monkeypatch.setattr(node_registry, "_REGISTRY_PATH", path)
    return path


@contextmanager
def statements_against_the_nodes_table():
    """Every SQL statement the block sends that names the ``nodes`` table.

    Counted at the driver rather than by patching the registry, so what is
    asserted is what the database is actually asked to do — on either backend,
    whose SQL differs in spelling but not in how many statements it takes.
    """
    from spark_pulse import db

    engine = db.engine()
    seen: list[str] = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if "nodes" in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", capture)


class TestIdentity:
    """Identity is minted, never derived. Plan section 3.1."""

    def test_minted_ids_are_random_and_unique(self):
        ids = {node_registry.mint_node_id() for _ in range(200)}
        assert len(ids) == 200
        assert all(len(node_id) == 32 for node_id in ids)

    def test_add_node_mints_the_id_and_ignores_hostname_and_machine_id(self):
        node = node_registry.add_node(
            name="spark-02", address="10.0.0.11", machine_id="deadbeef"
        )
        assert node.id not in ("spark-02", "10.0.0.11", "deadbeef")
        assert len(node.id) == 32

    def test_two_nodes_sharing_a_machine_id_still_get_distinct_identities(self):
        """The duplicate-machine-id defect must not collapse two nodes into one."""
        first = node_registry.add_node(address="10.0.0.11", machine_id="same")
        second = node_registry.add_node(address="10.0.0.12", machine_id="same")
        assert first.id != second.id
        assert {n.id for n in node_registry.list_nodes()} == {first.id, second.id}

    def test_renaming_and_readdressing_do_not_move_identity(self):
        node = node_registry.add_node(name="old", address="10.0.0.11")
        renamed = node_registry.update_node(node.id, name="new", address="10.0.0.99")
        assert renamed.id == node.id

    def test_the_id_cannot_be_changed(self):
        node = node_registry.add_node(address="10.0.0.11")
        with pytest.raises(ValueError, match="cannot change: id"):
            node_registry.update_node(node.id, id="something-else")

    def test_is_control_plane_cannot_be_changed(self):
        node = node_registry.add_node(address="10.0.0.11")
        with pytest.raises(ValueError, match="is_control_plane"):
            node_registry.update_node(node.id, is_control_plane=True)


class TestPersistence:
    """State is durable, and an unreadable file is not an empty cluster."""

    def test_nodes_survive_a_reload(self, registry_in_tmp):
        node = node_registry.add_node(
            name="spark-02",
            address="10.0.0.11",
            ssh_user="spark",
            ssh_key_path="/home/spark/.ssh/id_ed25519",
            ethernet_interface="enp1s0",
            infiniband_interfaces=["ib0", "ib1"],
            state="healthy",
        )

        reloaded = node_registry.get_node(node.id)
        assert reloaded == node
        assert reloaded.infiniband_interfaces == ("ib0", "ib1")

    def test_an_absent_file_is_an_empty_registry(self, registry_in_tmp):
        assert not registry_in_tmp.exists()
        assert node_registry.list_nodes() == []

    def test_an_unreadable_file_raises_rather_than_reporting_no_nodes(
        self, registry_in_tmp
    ):
        registry_in_tmp.write_text("{ not json")
        with pytest.raises(StateFileError):
            node_registry.list_nodes()

    def test_the_control_plane_sorts_first(self):
        node_registry.add_node(name="zulu", address="10.0.0.11")
        node_registry.add_node(name="alpha", address="10.0.0.12")
        control = node_registry.add_node(name="omega", is_control_plane=True)
        assert [n.name for n in node_registry.list_nodes()] == [
            control.name,
            "alpha",
            "zulu",
        ]

    def test_a_stored_node_keeps_every_field(self, registry_in_tmp):
        """What the ``version`` key used to guard, asserted directly.

        The JSON file carried a format version so a future reader could tell
        shapes apart. State is in the database now and the schema plays that
        role, so what is worth pinning here is the thing the version existed
        to protect: a record survives storage with every field intact.
        """
        node = node_registry.add_node(
            name="spark-02",
            address="10.0.0.11",
            ssh_user="spark",
            infiniband_interfaces=["rocep1s0f0", "rocep1s0f1"],
            fabric_mode="direct",
            state="healthy",
        )

        (stored,) = node_registry.list_nodes()

        assert stored == node
        assert stored.infiniband_interfaces == ("rocep1s0f0", "rocep1s0f1")
        assert stored.fabric_mode == "direct"

    def test_a_hand_edited_record_without_an_id_is_given_one(self, registry_in_tmp):
        registry_in_tmp.write_text(
            json.dumps({"version": 1, "nodes": [{"address": "10.0.0.11"}]})
        )
        (node,) = node_registry.list_nodes()
        assert len(node.id) == 32
        assert node.address == "10.0.0.11"


class TestPerRowWrites:
    """One change is one row.

    The first cut of the database store rewrote the whole registry on every
    write: renaming one peer read every node and merged every node back. That
    is a cost that grows with the cluster, and — worse — it makes a write
    depend on a set that was read before it, which is how a concurrent add is
    silently deleted. These pin the shape that replaced it.
    """

    @staticmethod
    def _writes(statements):
        return [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]

    def test_changing_one_node_costs_the_same_in_a_registry_of_three_and_of_forty(
        self,
    ):
        """The whole point: the price of an edit is the edit, not the cluster."""
        small = [node_registry.add_node(address=f"10.0.1.{i}") for i in range(1, 4)]
        with statements_against_the_nodes_table() as in_a_small_registry:
            node_registry.update_node(small[0].id, name="renamed")

        for i in range(1, 38):
            node_registry.add_node(address=f"10.0.2.{i}")
        with statements_against_the_nodes_table() as in_a_large_registry:
            node_registry.update_node(small[1].id, name="renamed-too")

        assert len(in_a_large_registry) == len(in_a_small_registry)
        assert len(self._writes(in_a_large_registry)) == 1

    def test_reading_one_node_costs_the_same_in_a_registry_of_three_and_of_forty(self):
        small = [node_registry.add_node(address=f"10.0.1.{i}") for i in range(1, 4)]
        with statements_against_the_nodes_table() as in_a_small_registry:
            assert node_registry.get_node(small[0].id) is not None

        for i in range(1, 38):
            node_registry.add_node(address=f"10.0.2.{i}")
        with statements_against_the_nodes_table() as in_a_large_registry:
            assert node_registry.get_node(small[1].id) is not None

        assert len(in_a_large_registry) == len(in_a_small_registry) == 1

    def test_forgetting_one_node_deletes_that_row_and_writes_no_other(self):
        """A whole-set save spelled "forget one" as "replace all but one"."""
        doomed = node_registry.add_node(address="10.0.0.11")
        node_registry.add_node(address="10.0.0.12")
        node_registry.add_node(address="10.0.0.13")

        with statements_against_the_nodes_table() as statements:
            node_registry.remove_node(doomed.id)

        (write,) = self._writes(statements)
        assert write.lstrip().upper().startswith("DELETE")
        assert len(node_registry.list_nodes()) == 2

    def test_nodes_added_from_several_threads_at_once_all_survive(self):
        """The lost update the whole-set save made possible, run for real.

        Each thread read the registry, appended its node and wrote the whole
        list back, so whichever thread wrote last erased every node registered
        while it was thinking. A cluster brought up by a script that enrolls
        its peers in parallel would end up with one of them.
        """
        addresses = [f"10.0.0.{i}" for i in range(2, 22)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            minted = {
                node.id
                for node in pool.map(
                    lambda address: node_registry.add_node(address=address), addresses
                )
            }

        assert len(minted) == len(addresses)
        assert {node.id for node in node_registry.list_nodes()} == minted

    def test_edits_to_different_nodes_from_different_threads_all_stick(self):
        nodes = [node_registry.add_node(address=f"10.0.0.{i}") for i in range(2, 12)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda node: node_registry.update_node(
                        node.id, name=f"renamed-{node.address}"
                    ),
                    nodes,
                )
            )

        assert {node.name for node in node_registry.list_nodes()} == {
            f"renamed-{node.address}" for node in nodes
        }

    def test_two_threads_racing_for_one_address_produce_one_node_not_two(self):
        """Per-row writes stop deleting the loser, so the check has to hold.

        Uniqueness used to be enforced by an accident: both threads inserted,
        and the second one's whole-set save deleted the first. Now that a write
        touches one row, "is this address free?" and the insert have to be one
        indivisible step or the registry ends up with two nodes at one address
        and pre-flight resolves it to whichever the query happened to return.
        """
        outcomes = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in [
                pool.submit(node_registry.add_node, address="10.0.0.11")
                for _ in range(8)
            ]:
                try:
                    outcomes.append(future.result())
                except ValueError as exc:
                    assert "already registered" in str(exc)

        assert len(outcomes) == 1
        assert [node.address for node in node_registry.list_nodes()] == ["10.0.0.11"]

    def test_two_threads_racing_to_register_the_control_plane_agree_on_one(self):
        """Two startups — the lifespan hook and a re-enrolling agent — racing."""
        outcomes = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for future in [
                pool.submit(
                    node_registry.add_node, name="control", is_control_plane=True
                )
                for _ in range(4)
            ]:
                try:
                    outcomes.append(future.result())
                except ValueError as exc:
                    assert "already registered" in str(exc)

        assert len(outcomes) == 1
        assert len([n for n in node_registry.list_nodes() if n.is_control_plane]) == 1


class TestWholeSetSave:
    """The bulk path is still there, and still means *replace*.

    Per-row writes are what a single change uses; a caller holding an entire
    registry — a reconciliation against an external source of truth, a restore
    — still needs "make the stored set be exactly this", including the
    deletions that implies.
    """

    def test_saving_a_set_deletes_the_nodes_absent_from_it(self):
        kept = node_registry.add_node(address="10.0.0.11")
        node_registry.add_node(address="10.0.0.12")

        node_registry._save([kept])

        assert [node.id for node in node_registry.list_nodes()] == [kept.id]

    def test_saving_a_set_inserts_and_updates_in_the_same_call(self):
        existing = node_registry.add_node(address="10.0.0.11", name="old")
        arriving = node_registry.NodeRecord(
            id=node_registry.mint_node_id(), name="new", address="10.0.0.12"
        )

        from dataclasses import replace as dataclass_replace

        node_registry._save([dataclass_replace(existing, name="renamed"), arriving])

        by_id = {node.id: node for node in node_registry.list_nodes()}
        assert by_id[existing.id].name == "renamed"
        assert by_id[arriving.id].name == "new"


class TestTheOneTimeImport:
    """``nodes.json`` is imported once, and "once" is a fact in ``meta``.

    Now that every entry point — including the writes — imports before it
    touches the table, the guard against a second import matters at more call
    sites than it did when only :func:`list_nodes` could trigger one.
    """

    def test_forgetting_the_last_node_does_not_resurrect_it_from_the_file(
        self, registry_in_tmp
    ):
        """The reason the key lives in ``meta`` and not in ``SELECT count(*)``.

        The file is deliberately left on disk after the import, so an import
        that keyed on an empty table would run again the moment an operator
        forgot the last node — and hand it straight back.
        """
        registry_in_tmp.write_text(
            json.dumps({"version": 1, "nodes": [{"address": "10.0.0.11"}]})
        )
        (imported,) = node_registry.list_nodes()

        node_registry.remove_node(imported.id)

        assert registry_in_tmp.exists()
        assert node_registry.list_nodes() == []
        assert node_registry.get_node(imported.id) is None

    def test_a_write_that_arrives_first_imports_the_file_before_it_checks(
        self, registry_in_tmp
    ):
        """Otherwise the import lands on top of the write it should have blocked.

        ``add_node`` refuses an address the registry already holds. If the
        first call after a restart were a write and it checked before the
        import, the file's node would not be there to collide with — and the
        import would then arrive and leave two nodes at 10.0.0.11.
        """
        registry_in_tmp.write_text(
            json.dumps({"version": 1, "nodes": [{"address": "10.0.0.11"}]})
        )

        with pytest.raises(ValueError, match="already registered"):
            node_registry.add_node(address="10.0.0.11")

        assert [node.address for node in node_registry.list_nodes()] == ["10.0.0.11"]

    def test_an_edit_made_after_the_import_is_not_undone_by_a_later_read(
        self, registry_in_tmp
    ):
        registry_in_tmp.write_text(
            json.dumps({"version": 1, "nodes": [{"address": "10.0.0.11"}]})
        )
        (imported,) = node_registry.list_nodes()
        node_registry.update_node(imported.id, address="192.168.50.4")

        assert [node.address for node in node_registry.list_nodes()] == ["192.168.50.4"]


class TestValidation:
    def test_a_peer_needs_an_address(self):
        with pytest.raises(ValueError, match="needs an address"):
            node_registry.add_node(name="nameless")

    def test_the_control_plane_may_have_no_address_yet(self):
        node = node_registry.add_node(name="control", is_control_plane=True)
        assert node.address == ""

    def test_an_address_is_registered_once(self):
        node_registry.add_node(address="10.0.0.11")
        with pytest.raises(ValueError, match="already registered"):
            node_registry.add_node(address="10.0.0.11")

    def test_an_update_cannot_collide_two_nodes_onto_one_address(self):
        node_registry.add_node(address="10.0.0.11")
        second = node_registry.add_node(address="10.0.0.12")
        with pytest.raises(ValueError, match="already registered"):
            node_registry.update_node(second.id, address="10.0.0.11")

    def test_there_is_only_one_control_plane(self):
        node_registry.add_node(name="a", is_control_plane=True)
        with pytest.raises(ValueError, match="already registered"):
            node_registry.add_node(name="b", is_control_plane=True)

    def test_state_must_be_one_of_the_three(self):
        node = node_registry.add_node(address="10.0.0.11")
        assert node.state == "unknown"
        with pytest.raises(ValueError, match="state must be one of"):
            node_registry.update_node(node.id, state="degraded")
        for state in node_registry.NODE_STATES:
            assert node_registry.update_node(node.id, state=state).state == state

    def test_removing_a_peer_forgets_it(self):
        node = node_registry.add_node(address="10.0.0.11")
        removed = node_registry.remove_node(node.id)
        assert removed.id == node.id
        assert node_registry.list_nodes() == []

    def test_the_control_plane_cannot_forget_itself(self):
        node = node_registry.add_node(name="control", is_control_plane=True)
        with pytest.raises(ValueError, match="control plane cannot be removed"):
            node_registry.remove_node(node.id)

    def test_removing_an_unknown_node_raises_key_error(self):
        with pytest.raises(KeyError):
            node_registry.remove_node("nope")


class TestRegisterSelf:
    """Idempotent across restarts, and it never overwrites an operator's edit."""

    @pytest.fixture(autouse=True)
    def discovered(self, monkeypatch):
        """A Spark as discovery sees it: one management link and two fabric ones."""
        monkeypatch.setattr(
            tools_package,
            "discovery",
            fake_discovery(
                detect_local_ip=lambda: "10.0.0.10",
                detect_network_interfaces=lambda: [
                    NetworkInterface(
                        name="enp1s0",
                        ip="10.0.0.10",
                        mtu=1500,
                        is_up=True,
                        type="ethernet",
                    ),
                    NetworkInterface(
                        name="ib0", ip=None, mtu=4096, is_up=True, type="infiniband"
                    ),
                    NetworkInterface(
                        name="ib1", ip=None, mtu=4096, is_up=True, type="infiniband"
                    ),
                    NetworkInterface(
                        name="docker0",
                        ip="172.17.0.1",
                        mtu=1500,
                        is_up=True,
                        type="docker",
                    ),
                ],
            ),
        )
        monkeypatch.setattr(node_registry, "read_machine_id", lambda: "mid-1")

    def test_first_run_creates_the_control_node_from_discovery(self):
        node = node_registry.register_self(name="spark-01")
        assert node.is_control_plane
        assert node.name == "spark-01"
        assert node.address == "10.0.0.10"
        assert node.ethernet_interface == "enp1s0"
        assert node.infiniband_interfaces == ("ib0", "ib1")
        assert node.state == "healthy"
        assert node.machine_id == "mid-1"
        assert node.last_seen

    def test_a_restart_does_not_add_a_second_control_node(self):
        first = node_registry.register_self(name="spark-01")
        second = node_registry.register_self(name="spark-01")
        assert second.id == first.id
        assert len(node_registry.list_nodes()) == 1

    def test_a_restart_does_not_overwrite_an_operators_edits(self):
        node = node_registry.register_self(name="spark-01")
        node_registry.update_node(
            node.id,
            name="fabric-head",
            address="192.168.50.4",
            ethernet_interface="eno1",
            infiniband_interfaces=["ib3"],
        )

        after = node_registry.register_self(name="spark-01")
        assert after.name == "fabric-head"
        assert after.address == "192.168.50.4"
        assert after.ethernet_interface == "eno1"
        assert after.infiniband_interfaces == ("ib3",)

    def test_a_restart_fills_a_blank_discovery_could_not_supply_before(self):
        node = node_registry.add_node(name="control", is_control_plane=True)
        assert node.address == ""
        after = node_registry.register_self()
        assert after.id == node.id
        assert after.address == "10.0.0.10"

    def test_the_machine_id_is_refreshed_but_is_not_the_identity(self, monkeypatch):
        node = node_registry.register_self(name="spark-01")
        monkeypatch.setattr(node_registry, "read_machine_id", lambda: "mid-2")
        after = node_registry.register_self(name="spark-01")
        assert after.id == node.id
        assert after.machine_id == "mid-2"


class TestDiagnostics:
    """Findings with a remedy, never exceptions. Plan section 8."""

    @pytest.fixture(autouse=True)
    def quiet_discovery(self, monkeypatch):
        """A clean network: no churn, link-local everywhere, mDNS available."""
        module = fake_discovery(
            detect_network_interfaces=lambda: [
                NetworkInterface(
                    name="enp1s0", ip="10.0.0.10", mtu=1500, is_up=True, type="ethernet"
                ),
                NetworkInterface(
                    name="ib0", ip=None, mtu=4096, is_up=True, type="infiniband"
                ),
            ],
            detect_link_local_addresses=lambda: {
                "enp1s0": "fe80::1",
                "ib0": "fe80::2",
            },
        )
        monkeypatch.setattr(tools_package, "discovery", module)
        return module

    def test_a_healthy_cluster_produces_no_findings(self):
        node_registry.add_node(address="10.0.0.11", machine_id="a")
        node_registry.add_node(address="10.0.0.12", machine_id="b")
        assert node_registry.diagnose() == []

    def test_duplicate_machine_ids_are_reported_with_their_remedy(self):
        first = node_registry.add_node(
            name="spark-01", address="10.0.0.11", machine_id="same"
        )
        second = node_registry.add_node(
            name="spark-02", address="10.0.0.12", machine_id="same"
        )
        (finding,) = node_registry.diagnose()
        assert finding.code == "duplicate_machine_id"
        assert finding.severity == "warning"
        assert set(finding.node_ids) == {first.id, second.id}
        assert "spark-01" in finding.summary and "spark-02" in finding.summary
        assert "systemd-machine-id-setup" in finding.remedy
        # It says enrollment is unaffected, because identity is minted.
        assert "enrollment is unaffected" in finding.remedy

    def test_an_empty_machine_id_is_not_a_duplicate(self):
        node_registry.add_node(address="10.0.0.11")
        node_registry.add_node(address="10.0.0.12")
        assert node_registry.diagnose() == []

    def test_mdns_hostname_churn_is_reported(self, quiet_discovery, monkeypatch):
        monkeypatch.setattr(
            quiet_discovery,
            "mdns_hostname_history",
            lambda: {"10.0.0.11": {"spark-02.local", "dgx-spark.local"}},
        )
        (finding,) = node_registry.diagnose()
        assert finding.code == "mdns_hostname_churn"
        assert "10.0.0.11" in finding.summary
        assert "dgx-spark.local" in finding.summary
        assert "hostnamectl" in finding.remedy

    def test_one_hostname_per_address_is_not_churn(self, quiet_discovery, monkeypatch):
        monkeypatch.setattr(
            quiet_discovery,
            "mdns_hostname_history",
            lambda: {"10.0.0.11": {"spark-02.local"}},
        )
        assert node_registry.diagnose() == []

    def test_an_interface_without_a_link_local_address_is_reported(
        self, quiet_discovery, monkeypatch
    ):
        """`link-local: []` in a netplan profile silently kills peer sweeps."""
        monkeypatch.setattr(
            quiet_discovery,
            "detect_link_local_addresses",
            lambda: {"enp1s0": "fe80::1"},
        )
        (finding,) = node_registry.diagnose()
        assert finding.code == "interface_no_link_local"
        assert "ib0" in finding.summary
        assert "link-local: []" in finding.remedy
        assert "netplan apply" in finding.remedy

    def test_unavailable_mdns_is_information_not_a_failure(
        self, quiet_discovery, monkeypatch
    ):
        monkeypatch.setattr(quiet_discovery, "mdns_available", lambda: False)
        (finding,) = node_registry.diagnose()
        assert finding.code == "mdns_unavailable"
        assert finding.severity == "info"
        assert "adding a node by address always works" in finding.remedy

    def test_a_discovery_failure_does_not_break_diagnostics(
        self, quiet_discovery, monkeypatch
    ):
        def explode():
            raise OSError("no network")

        monkeypatch.setattr(quiet_discovery, "detect_network_interfaces", explode)
        monkeypatch.setattr(quiet_discovery, "mdns_hostname_history", explode)
        monkeypatch.setattr(quiet_discovery, "mdns_available", explode)
        assert node_registry.diagnose() == []


class TestMachineId:
    def test_reading_a_missing_machine_id_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(node_registry, "_MACHINE_ID_FILES", (tmp_path / "absent",))
        assert node_registry.read_machine_id() == ""

    def test_the_first_readable_file_wins(self, tmp_path, monkeypatch):
        second = tmp_path / "second"
        second.write_text("  abc123\n")
        monkeypatch.setattr(
            node_registry, "_MACHINE_ID_FILES", (tmp_path / "absent", second)
        )
        assert node_registry.read_machine_id() == "abc123"


class TestFabricFromDiscovery:
    """The CX7 fabric, which is not what the generic interface scan sees.

    On a DGX Spark the RoCE devices ``NCCL_IB_HCA`` names live in
    ``/sys/class/infiniband`` — ``rocep1s0f1`` — while the netdev each drives
    is ``enp1s0f1np1``, which a name-prefix scan classifies as plain ethernet.
    So the scan finds no fabric at all on a real Spark, and the record has to
    come from ``ibdev2netdev``.
    """

    ONE_CABLE = (
        "rocep1s0f0 port 1 ==> enp1s0f0np0 (Down)\n"
        "rocep1s0f1 port 1 ==> enp1s0f1np1 (Up)\n"
        "roceP2p1s0f0 port 1 ==> enP2p1s0f0np0 (Down)\n"
        "roceP2p1s0f1 port 1 ==> enP2p1s0f1np1 (Up)\n"
        "== addr\n"
        "2: enp1s0f1np1    inet 192.168.177.11/24 scope global enp1s0f1np1\n"
        "3: enP2p1s0f1np1    inet 192.168.178.11/24 scope global enP2p1s0f1np1"
    )

    @pytest.fixture
    def spark(self, monkeypatch):
        from spark_pulse.tools.discovery import fabric_from_output

        monkeypatch.setattr(
            tools_package,
            "discovery",
            fake_discovery(
                detect_local_ip=lambda: "10.0.0.10",
                detect_fabric=lambda: fabric_from_output(self.ONE_CABLE),
                detect_network_interfaces=lambda: [
                    # The netdevs a scan would see. None of them is the RoCE
                    # device, which is exactly the point.
                    NetworkInterface(
                        name="enP7s7",
                        ip="10.0.0.10",
                        mtu=1500,
                        is_up=True,
                        type="ethernet",
                    ),
                    NetworkInterface(
                        name="enp1s0f1np1",
                        ip="192.168.177.11",
                        mtu=9000,
                        is_up=True,
                        type="ethernet",
                    ),
                ],
            ),
        )
        monkeypatch.setattr(node_registry, "read_machine_id", lambda: "mid-1")

    def test_the_record_holds_both_roce_twins(self, spark):
        node = node_registry.register_self(name="spark-01")
        assert node.infiniband_interfaces == ("rocep1s0f1", "roceP2p1s0f1")

    def test_the_management_link_is_the_addressed_twin_not_the_first_scanned(
        self, spark
    ):
        """The scan would have taken ``enP7s7``; upstream takes the fabric's."""
        node = node_registry.register_self(name="spark-01")
        assert node.ethernet_interface == "enp1s0f1np1"

    def test_the_cabling_is_recorded(self, spark):
        node = node_registry.register_self(name="spark-01")
        assert node.fabric_mode == "direct"

    def test_a_machine_with_no_fabric_falls_back_to_the_interface_scan(
        self, monkeypatch
    ):
        """IPoIB machines and developer laptops keep the old behaviour."""
        monkeypatch.setattr(
            tools_package,
            "discovery",
            fake_discovery(
                detect_local_ip=lambda: "10.0.0.10",
                detect_network_interfaces=lambda: [
                    NetworkInterface(
                        name="enp1s0",
                        ip="10.0.0.10",
                        mtu=1500,
                        is_up=True,
                        type="ethernet",
                    ),
                    NetworkInterface(
                        name="ib0", ip=None, mtu=4096, is_up=True, type="infiniband"
                    ),
                ],
            ),
        )
        monkeypatch.setattr(node_registry, "read_machine_id", lambda: "mid-1")

        node = node_registry.register_self(name="spark-01")
        assert node.ethernet_interface == "enp1s0"
        assert node.infiniband_interfaces == ("ib0",)
        assert node.fabric_mode == ""


class TestFabricMode:
    def test_it_survives_a_round_trip(self):
        node = node_registry.add_node(address="10.0.0.9", fabric_mode="mesh")
        assert node_registry.get_node(node.id).fabric_mode == "mesh"

    def test_an_unknown_mode_is_refused_on_update(self):
        node = node_registry.add_node(address="10.0.0.9")
        with pytest.raises(ValueError, match="fabric_mode"):
            node_registry.update_node(node.id, fabric_mode="daisy-chain")

    def test_it_can_be_cleared(self):
        node = node_registry.add_node(address="10.0.0.9", fabric_mode="mesh")
        assert node_registry.update_node(node.id, fabric_mode="").fabric_mode == ""

    def test_a_hand_edited_file_with_nonsense_reads_back_as_unknown(self):
        """A registry we cannot parse is worse than a field we ignore."""
        record = node_registry.NodeRecord.from_dict(
            {"id": "x", "address": "10.0.0.9", "fabric_mode": "banana"}
        )
        assert record.fabric_mode == ""
