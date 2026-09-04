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

import pytest

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
        assert registry_in_tmp.exists()

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

    def test_the_file_records_a_version(self, registry_in_tmp):
        node_registry.add_node(address="10.0.0.11")
        data = json.loads(registry_in_tmp.read_text())
        assert data["version"] == 1
        assert len(data["nodes"]) == 1

    def test_a_hand_edited_record_without_an_id_is_given_one(self, registry_in_tmp):
        registry_in_tmp.write_text(
            json.dumps({"version": 1, "nodes": [{"address": "10.0.0.11"}]})
        )
        (node,) = node_registry.list_nodes()
        assert len(node.id) == 32
        assert node.address == "10.0.0.11"


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
