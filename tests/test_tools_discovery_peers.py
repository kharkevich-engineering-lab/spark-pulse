"""Peer discovery: scoped to real interfaces, and never fatal.

Two properties are load-bearing and both come out of the Spark test recorded in
``docs/cluster-agent-plan.md`` section 6. Announcing and browsing must be
restricted to real interfaces, because browsing everything announced the
service on the docker bridge and on a veth pair. And discovery must degrade to
an empty list rather than raising, because manual entry has to keep working
when mDNS does not.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

import spark_pulse.tools.discovery  # noqa: F401 — the real submodule
from spark_pulse.tools.discovery import DiscoveredPeer, NetworkInterface

discovery = sys.modules["spark_pulse.tools.discovery"]


def interface(name, ip=None, is_up=True, kind="ethernet", mtu=1500):
    return NetworkInterface(name=name, ip=ip, mtu=mtu, is_up=is_up, type=kind)


#: A DGX Spark as it actually looks: management link, two fabric links, plus
#: the container plumbing that must never carry an announcement.
SPARK_INTERFACES = [
    interface("lo", "127.0.0.1", kind="loopback", mtu=65536),
    interface("enp1s0", "10.0.0.10"),
    interface("ib0", "169.254.1.1", kind="infiniband", mtu=4096),
    interface("ib1", None, kind="infiniband", mtu=4096, is_up=False),
    interface("docker0", "172.17.0.1", kind="docker"),
    interface("br-9f21ac", "172.18.0.1", kind="docker"),
    interface("veth3a1b2c", None, kind="other"),
]


@pytest.fixture
def spark_host(monkeypatch):
    monkeypatch.setattr(
        discovery, "detect_network_interfaces", lambda: list(SPARK_INTERFACES)
    )
    monkeypatch.setattr(discovery, "ibdev2netdev_up", list)


class TestInterfaceScoping:
    def test_containers_and_loopback_are_never_real(self, spark_host):
        assert discovery.real_interface_names() == ["enp1s0", "ib0"]

    def test_a_down_interface_is_not_announced_on(self, spark_host):
        assert "ib1" not in discovery.real_interface_names()

    def test_only_addressable_interfaces_are_handed_to_zeroconf(self, spark_host):
        assert discovery.announce_addresses() == ["10.0.0.10", "169.254.1.1"]

    def test_ibdev2netdev_decides_which_fabric_links_are_up(
        self, spark_host, monkeypatch
    ):
        """The DGX tool overrules operstate when it answers at all."""
        monkeypatch.setattr(discovery, "ibdev2netdev_up", lambda: ["ib1"])
        assert discovery.real_interface_names() == ["enp1s0"]

    def test_a_named_virtual_interface_is_excluded_whatever_its_class(self):
        for name in ("veth123", "virbr0", "cni0", "tap0", "podman1"):
            assert not discovery.is_real_interface(interface(name, "10.9.9.9"))


class TestIbdev2Netdev:
    def test_up_ports_are_parsed(self, monkeypatch):
        output = (
            "mlx5_0 port 1 ==> ib0 (Up)\n"
            "mlx5_1 port 1 ==> ib1 (Down)\n"
            "mlx5_2 port 1 ==> ib2 (Up)\n"
        )
        monkeypatch.setattr(
            discovery.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, output, ""),
        )
        assert discovery.ibdev2netdev_up() == ["ib0", "ib2"]

    def test_a_missing_tool_is_not_an_error(self, monkeypatch):
        def missing(*_args, **_kwargs):
            raise FileNotFoundError("ibdev2netdev")

        monkeypatch.setattr(discovery.subprocess, "run", missing)
        assert discovery.ibdev2netdev_up() == []

    def test_a_nonzero_exit_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(
            discovery.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "no devices"),
        )
        assert discovery.ibdev2netdev_up() == []


class TestLinkLocal:
    def test_proc_parsing_finds_the_fe80_address(self, tmp_path, monkeypatch):
        proc = tmp_path / "if_inet6"
        proc.write_text(
            "00000000000000000000000000000001 01 80 10 80       lo\n"
            "fe800000000000000a00270ffe1a2b3c 02 40 20 80   enp1s0\n"
        )
        monkeypatch.setattr(discovery, "Path", lambda _p: proc)
        found = discovery._link_local_from_proc()
        assert found == {"enp1s0": "fe80:0000:0000:0000:0a00:270f:fe1a:2b3c"}

    def test_an_absent_proc_file_is_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(discovery, "Path", lambda _p: tmp_path / "absent")
        assert discovery._link_local_from_proc() == {}


class TestBrowseDegradesGracefully:
    def test_no_zeroconf_means_no_peers_and_no_exception(self, monkeypatch):
        monkeypatch.setattr(discovery, "_try_import_zeroconf", lambda: None)
        assert discovery.browse_peers(timeout=0.1) == []
        assert discovery.mdns_available() is False
        assert discovery.announce_self("abc", 8100, "1.0") is False

    def test_no_addressable_interface_means_no_peers(self, monkeypatch):
        monkeypatch.setattr(discovery, "announce_addresses", list)
        assert discovery.browse_peers(timeout=0.1) == []
        assert discovery.announce_self("abc", 8100, "1.0") is False

    def test_a_failing_zeroconf_is_reported_as_no_peers(self, monkeypatch):
        class Exploding:
            IPVersion = type("IPVersion", (), {"V4Only": 1})

            @staticmethod
            def Zeroconf(**_kwargs):
                raise OSError("cannot bind 5353")

        monkeypatch.setattr(discovery, "announce_addresses", lambda: ["10.0.0.10"])
        monkeypatch.setattr(discovery, "_try_import_zeroconf", lambda: Exploding)
        assert discovery.browse_peers(timeout=0.1) == []
        assert discovery.announce_self("abc", 8100, "1.0") is False


class TestHostnameHistory:
    def test_churn_is_visible_across_browses(self):
        discovery.reset_mdns_history()
        try:
            discovery.record_mdns_hostname("10.0.0.11", "spark-02.local.")
            discovery.record_mdns_hostname("10.0.0.11", "dgx-spark.local.")
            discovery.record_mdns_hostname("10.0.0.12", "spark-03.local.")
            history = discovery.mdns_hostname_history()
            assert history["10.0.0.11"] == {"spark-02.local", "dgx-spark.local"}
            assert history["10.0.0.12"] == {"spark-03.local"}
        finally:
            discovery.reset_mdns_history()

    def test_blank_observations_are_dropped(self):
        discovery.reset_mdns_history()
        discovery.record_mdns_hostname("", "spark.local")
        discovery.record_mdns_hostname("10.0.0.11", "")
        assert discovery.mdns_hostname_history() == {}


class TestTxtDecoding:
    def test_bytes_keys_and_values_become_strings(self):
        decoded = discovery._decode_txt(
            {b"node_id": b"abc123", b"version": b"1.2.3", b"flag": None}
        )
        assert decoded == {"node_id": "abc123", "version": "1.2.3", "flag": ""}


class TestMockTwin:
    """The mock must answer the same questions with the same shapes."""

    def test_the_canned_peers_distinguish_spark_pulse_from_bare_ssh(self):
        from spark_pulse.mock import discovery as mock_discovery

        mock_discovery.reset_mock_discovery()
        peers = mock_discovery.browse_peers(timeout=0.1)
        assert all(isinstance(peer, DiscoveredPeer) for peer in peers)

        ours = [p for p in peers if p.is_spark_pulse]
        theirs = [p for p in peers if not p.is_spark_pulse]
        assert ours and ours[0].node_id and ours[0].version
        # An `_ssh._tcp` answer says a host exists and nothing more.
        assert theirs and not theirs[0].node_id

    def test_unavailable_mdns_yields_an_empty_list(self):
        from spark_pulse.mock import discovery as mock_discovery

        mock_discovery.reset_mock_discovery()
        try:
            mock_discovery.set_mdns_available(False)
            assert mock_discovery.browse_peers(timeout=0.1) == []
            assert mock_discovery.mdns_available() is False
            assert mock_discovery.announce_self("abc", 8100, "1.0") is False
        finally:
            mock_discovery.reset_mock_discovery()

    def test_the_mock_excludes_container_interfaces_too(self):
        from spark_pulse.mock import discovery as mock_discovery

        mock_discovery.reset_mock_discovery()
        names = mock_discovery.real_interface_names()
        assert "eth0" in names
        assert "docker0" not in names and "lo" not in names

    def test_both_modules_expose_the_same_peer_surface(self):
        from spark_pulse.mock import discovery as mock_discovery

        for name in (
            "browse_peers",
            "announce_self",
            "stop_announcement",
            "mdns_available",
            "mdns_hostname_history",
            "reset_mdns_history",
            "record_mdns_hostname",
            "real_interfaces",
            "real_interface_names",
            "announce_addresses",
            "detect_link_local_addresses",
            "ibdev2netdev_up",
            "is_real_interface",
        ):
            assert hasattr(discovery, name), f"real discovery lacks {name}"
            assert hasattr(mock_discovery, name), f"mock discovery lacks {name}"


class TestRunDiscoveryIsNotFrozen:
    """``run_discovery`` assigned to a frozen dataclass and raised every time."""

    def test_the_real_run_discovery_returns_nccl_defaults(self, spark_host):
        result = discovery.run_discovery()
        assert result.ethernet_if == "enp1s0"
        assert result.nccl_defaults is not None
        assert result.nccl_defaults.socket_ifname == "enp1s0"
