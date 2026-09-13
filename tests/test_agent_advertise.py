"""The listener certificate answers to every name this machine can be dialled by."""

from __future__ import annotations

import pytest

from spark_pulse.agent import advertise
from spark_pulse.agent.advertise import advertised_names, split_names
from spark_pulse.agent.identity import certificate_names
from spark_pulse.agent.server import ControlPlaneServer
from spark_pulse.tools.discovery import NetworkInterface


class TestSplitNames:
    def test_addresses_and_hostnames_are_told_apart(self):
        assert split_names(
            ["localhost", "192.168.29.60", "gx10-ced2.local", "::1"]
        ) == (
            ["localhost", "gx10-ced2.local"],
            ["192.168.29.60", "::1"],
        )

    def test_prefixes_zones_and_case_are_normalised(self):
        dns, ips = split_names(["10.0.0.1/24", "fe80::1%eth0", "Spark-01.", "spark-01"])
        assert ips == ["10.0.0.1", "fe80::1"]
        assert dns == ["spark-01"]

    def test_duplicates_and_junk_are_dropped(self):
        dns, ips = split_names(
            ["a", "a", "", "  ", "not a host name", "1.2.3.4", "1.2.3.4"]
        )
        assert dns == ["a"]
        assert ips == ["1.2.3.4"]


class TestAdvertisedNames:
    @pytest.fixture
    def machine(self, monkeypatch):
        """A machine with two interfaces, a default-route address and a registry entry."""
        from spark_pulse.tools import discovery, node_registry

        monkeypatch.setattr(advertise.socket, "gethostname", lambda: "gx10-ced2")
        monkeypatch.setattr(
            discovery,
            "detect_network_interfaces",
            lambda: [
                NetworkInterface("wlP9s9", "192.168.29.60", 1500, True, "other"),
                NetworkInterface("enP7s7", None, 1500, False, "ethernet"),
                NetworkInterface("docker0", "172.17.0.1", 1500, True, "docker"),
            ],
        )
        monkeypatch.setattr(discovery, "detect_local_ip", lambda: "192.168.29.60")

        class Me:
            address = "10.42.0.1"

        monkeypatch.setattr(node_registry, "self_node", lambda: Me())

    def test_every_source_contributes(self, machine):
        dns, ips = advertised_names()
        assert dns == ["localhost", "gx10-ced2", "gx10-ced2.local"]
        assert ips == ["127.0.0.1", "::1", "192.168.29.60", "172.17.0.1", "10.42.0.1"]

    def test_extra_names_are_taken(self, machine):
        dns, ips = advertised_names(["cp.example.net", "203.0.113.7"])
        assert "cp.example.net" in dns
        assert "203.0.113.7" in ips

    def test_a_failing_discovery_loses_only_its_share(self, machine, monkeypatch):
        from spark_pulse.tools import discovery

        def boom():
            raise RuntimeError("no /sys here")

        monkeypatch.setattr(discovery, "detect_network_interfaces", boom)
        dns, ips = advertised_names()
        assert dns == ["localhost", "gx10-ced2", "gx10-ced2.local"]
        assert ips == [
            "127.0.0.1",
            "::1",
            "10.42.0.1",
        ], "the registry's address survives"

    def test_loopback_is_always_there(self, monkeypatch):
        from spark_pulse.tools import discovery, node_registry

        monkeypatch.setattr(advertise.socket, "gethostname", lambda: "")
        monkeypatch.setattr(discovery, "detect_network_interfaces", lambda: [])
        monkeypatch.setattr(discovery, "detect_local_ip", lambda: None)
        monkeypatch.setattr(node_registry, "self_node", lambda: None)
        assert advertised_names() == (["localhost"], ["127.0.0.1", "::1"])


class TestTheCertificate:
    def test_the_listener_certificate_carries_the_names_it_was_given(self, tmp_path):
        server = ControlPlaneServer(
            directory=tmp_path,
            dns_names=["localhost", "gx10-ced2"],
            ip_addresses=["127.0.0.1", "192.168.29.60", "not-an-address"],
        )
        assert server.names == ["localhost", "gx10-ced2", "127.0.0.1", "192.168.29.60"]

    def test_without_names_it_answers_to_loopback_only(self, tmp_path):
        """The default, which is what refused the first peer in the field."""
        server = ControlPlaneServer(directory=tmp_path)
        assert server.names == ["localhost", "127.0.0.1", "::1"]

    def test_a_certificate_without_sans_has_no_names(self, tmp_path):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from datetime import datetime, timedelta, timezone

        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "bare")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        assert certificate_names(cert.public_bytes(serialization.Encoding.PEM)) == []
