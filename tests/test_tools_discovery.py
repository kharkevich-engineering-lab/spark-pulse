"""Tests for network discovery tool module (simulation mode)."""

import pytest
from unittest.mock import patch

from spark_pulse.tools import discovery, is_simulation
from spark_pulse.tools.discovery import (
    NetworkInterface,
    InfinibandDevice,
    NCCLConfig,
    DiscoveryResult,
    ValidationResult,
    build_nccl_defaults,
    validate_network,
)


class TestDiscoveryModuleImport:
    """Test that discovery module is properly accessible."""

    def test_module_has_expected_functions(self):
        """Test that the module exports expected functions."""
        assert hasattr(discovery, "run_discovery")
        assert hasattr(discovery, "detect_network_interfaces")
        assert hasattr(discovery, "detect_local_ip")
        assert hasattr(discovery, "detect_infiniband_devices")
        assert hasattr(discovery, "build_nccl_defaults")
        assert hasattr(discovery, "validate_network")

    def test_is_simulation_returns_bool(self):
        """Test that is_simulation returns a boolean."""
        result = is_simulation()
        assert isinstance(result, bool)


class TestDataModels:
    """Test data model creation and serialization."""

    def test_network_interface_creation(self):
        """Test NetworkInterface dataclass creation."""
        iface = NetworkInterface(
            name="eth0",
            ip="192.168.1.100",
            mtu=1500,
            is_up=True,
            type="ethernet",
        )
        assert iface.name == "eth0"
        assert iface.ip == "192.168.1.100"
        assert iface.mtu == 1500
        assert iface.is_up is True
        assert iface.type == "ethernet"

    def test_network_interface_frozen(self):
        """Test that NetworkInterface is immutable (frozen)."""
        iface = NetworkInterface(
            name="eth0",
            ip="192.168.1.100",
            mtu=1500,
            is_up=True,
            type="ethernet",
        )
        with pytest.raises(Exception):
            iface.name = "eth1"

    def test_infiniband_device_creation(self):
        """Test InfinibandDevice dataclass creation."""
        dev = InfinibandDevice(
            hca="mlx5_0",
            ports=[1, 2],
            net_devices=["ib0"],
            state="ACTIVE",
        )
        assert dev.hca == "mlx5_0"
        assert dev.ports == [1, 2]
        assert dev.net_devices == ["ib0"]
        assert dev.state == "ACTIVE"

    def test_nccl_config_creation(self):
        """Test NCCLConfig dataclass creation."""
        nccl = NCCLConfig(
            socket_ifname="eth0",
            ib_hca="mlx5_0",
            ib_disable=False,
        )
        assert nccl.socket_ifname == "eth0"
        assert nccl.ib_hca == "mlx5_0"
        assert nccl.ib_disable is False

    def test_nccl_config_ib_disabled(self):
        """Test NCCLConfig with IB disabled."""
        nccl = NCCLConfig(
            socket_ifname="eth0",
            ib_hca=None,
            ib_disable=True,
        )
        assert nccl.ib_hca is None
        assert nccl.ib_disable is True

    def test_discovery_result_creation(self):
        """Test DiscoveryResult dataclass creation."""
        result = DiscoveryResult(
            local_ip="192.168.1.100",
            ethernet_if="eth0",
            infiniband_present=False,
            infiniband_devices=[],
            interfaces=[],
            nccl_defaults=NCCLConfig(
                socket_ifname="eth0", ib_hca=None, ib_disable=True
            ),
        )
        assert result.local_ip == "192.168.1.100"
        assert result.ethernet_if == "eth0"
        assert result.infiniband_present is False

    def test_validation_result_creation(self):
        """Test ValidationResult dataclass creation."""
        result = ValidationResult(
            healthy=True,
            warnings=["warning1"],
            errors=[],
        )
        assert result.healthy is True
        assert result.warnings == ["warning1"]
        assert result.errors == []


class TestInterfaceClassification:
    """Test network interface classification logic."""

    def test_ethernet_interface(self):
        """Test ethernet interface classification."""
        from spark_pulse.tools.discovery import _classify_interface

        assert _classify_interface("eth0") == "ethernet"
        assert _classify_interface("eth1") == "ethernet"
        assert _classify_interface("enp3s0") == "ethernet"

    def test_infiniband_interface(self):
        """Test infiniband interface classification."""
        from spark_pulse.tools.discovery import _classify_interface

        assert _classify_interface("ib0") == "infiniband"
        assert _classify_interface("ib1") == "infiniband"
        assert _classify_interface("mlx5_0") == "infiniband"

    def test_loopback_interface(self):
        """Test loopback interface classification."""
        from spark_pulse.tools.discovery import _classify_interface

        assert _classify_interface("lo") == "loopback"

    def test_docker_interface(self):
        """Test docker interface classification."""
        from spark_pulse.tools.discovery import _classify_interface

        assert _classify_interface("docker0") == "docker"
        assert _classify_interface("br-abc123") == "docker"

    def test_unknown_interface(self):
        """Test unknown interface classification."""
        from spark_pulse.tools.discovery import _classify_interface

        assert _classify_interface("wlan0") == "other"
        assert _classify_interface("veth123") == "other"


class TestDiscoverySimulation:
    """Test discovery functions in simulation mode."""

    def test_run_discovery_simulation(self):
        """Test run_discovery in simulation mode returns DiscoveryResult."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        result = discovery.run_discovery()
        assert isinstance(result, DiscoveryResult)
        assert isinstance(result.interfaces, list)
        assert isinstance(result.infiniband_devices, list)
        assert isinstance(result.validation_errors, list)

    def test_run_discovery_has_ethernet(self):
        """Test discovery detects at least one ethernet interface in simulation."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        result = discovery.run_discovery()
        ethernet_ifs = [i for i in result.interfaces if i.type == "ethernet"]
        assert len(ethernet_ifs) > 0, "Expected at least one ethernet interface"

    def test_nccl_defaults_generated(self):
        """Test NCCL defaults are generated from discovery results."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        result = discovery.run_discovery()
        assert result.nccl_defaults is not None
        assert result.nccl_defaults.socket_ifname is not None

    def test_detect_network_interfaces(self):
        """Test detect_network_interfaces returns list of NetworkInterface."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        interfaces = discovery.detect_network_interfaces()
        assert isinstance(interfaces, list)
        for iface in interfaces:
            assert isinstance(iface, NetworkInterface)

    def test_detect_local_ip(self):
        """Test detect_local_ip returns string or None."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        ip = discovery.detect_local_ip()
        assert ip is None or isinstance(ip, str)

    def test_detect_infiniband_devices(self):
        """Test detect_infiniband_devices returns list."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        devices = discovery.detect_infiniband_devices()
        assert isinstance(devices, list)
        for dev in devices:
            assert isinstance(dev, InfinibandDevice)

    def test_validate_network(self):
        """Test validate_network returns ValidationResult."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        result = discovery.validate_network()
        assert isinstance(result, ValidationResult)
        assert isinstance(result.healthy, bool)
        assert isinstance(result.warnings, list)
        assert isinstance(result.errors, list)


class TestBuildNcclDefaults:
    """Test NCCL default generation logic."""

    def test_ethernet_only_no_ib(self):
        """Test NCCL defaults when only ethernet is available."""
        interfaces = [
            NetworkInterface(
                name="eth0", ip="192.168.1.100", mtu=1500, is_up=True, type="ethernet"
            ),
        ]
        discovery_result = DiscoveryResult(
            local_ip="192.168.1.100",
            ethernet_if="eth0",
            infiniband_present=False,
            infiniband_devices=[],
            interfaces=interfaces,
            nccl_defaults=None,
        )
        nccl = build_nccl_defaults(discovery_result)
        assert nccl is not None
        assert nccl.socket_ifname == "eth0"
        assert nccl.ib_hca is None
        assert nccl.ib_disable is True

    def test_ethernet_with_active_ib(self):
        """Test NCCL defaults when ethernet + active IB available."""
        interfaces = [
            NetworkInterface(
                name="eth0", ip="10.0.0.10", mtu=1500, is_up=True, type="ethernet"
            ),
            NetworkInterface(
                name="ib0", ip=None, mtu=4096, is_up=True, type="infiniband"
            ),
        ]
        ib_devices = [
            InfinibandDevice(
                hca="mlx5_0", ports=[1, 2], net_devices=["ib0"], state="ACTIVE"
            ),
        ]
        discovery_result = DiscoveryResult(
            local_ip="10.0.0.10",
            ethernet_if="eth0",
            infiniband_present=True,
            infiniband_devices=ib_devices,
            interfaces=interfaces,
            nccl_defaults=None,
        )
        nccl = build_nccl_defaults(discovery_result)
        assert nccl is not None
        assert nccl.socket_ifname == "eth0"
        assert nccl.ib_hca == "mlx5_0"
        assert nccl.ib_disable is False

    def test_ethernet_with_down_ib(self):
        """Test NCCL defaults when IB is present but down."""
        interfaces = [
            NetworkInterface(
                name="eth0", ip="192.168.1.50", mtu=1500, is_up=True, type="ethernet"
            ),
        ]
        ib_devices = [
            InfinibandDevice(
                hca="mlx5_0", ports=[1, 2], net_devices=["ib0"], state="DOWN"
            ),
        ]
        discovery_result = DiscoveryResult(
            local_ip="192.168.1.50",
            ethernet_if="eth0",
            infiniband_present=True,
            infiniband_devices=ib_devices,
            interfaces=interfaces,
            nccl_defaults=None,
        )
        nccl = build_nccl_defaults(discovery_result)
        assert nccl is not None
        assert nccl.socket_ifname == "eth0"
        assert nccl.ib_hca is None
        assert nccl.ib_disable is True

    def test_no_ethernet_interface(self):
        """Test NCCL defaults when no ethernet interface is available."""
        interfaces = [
            NetworkInterface(
                name="lo", ip="127.0.0.1", mtu=65536, is_up=True, type="loopback"
            ),
        ]
        discovery_result = DiscoveryResult(
            local_ip=None,
            ethernet_if=None,
            infiniband_present=False,
            infiniband_devices=[],
            interfaces=interfaces,
            nccl_defaults=None,
        )
        nccl = build_nccl_defaults(discovery_result)
        assert nccl is None


class TestValidation:
    """Test network validation logic."""

    def test_healthy_network(self):
        """Test validation for a healthy network."""
        with (
            patch(
                "spark_pulse.tools.discovery.detect_network_interfaces"
            ) as mock_ifaces,
            patch("spark_pulse.tools.discovery.detect_local_ip") as mock_ip,
            patch("spark_pulse.tools.discovery.detect_infiniband_devices") as mock_ib,
            patch("spark_pulse.tools.discovery._port_available") as mock_port,
        ):
            mock_ifaces.return_value = [
                NetworkInterface(
                    name="eth0",
                    ip="192.168.1.100",
                    mtu=1500,
                    is_up=True,
                    type="ethernet",
                ),
                NetworkInterface(
                    name="lo", ip="127.0.0.1", mtu=65536, is_up=True, type="loopback"
                ),
            ]
            mock_ip.return_value = "192.168.1.100"
            mock_ib.return_value = []
            mock_port.return_value = True

            result = validate_network()
            assert result.healthy is True
            assert isinstance(result.warnings, list)
            assert isinstance(result.errors, list)

    def test_missing_ethernet(self):
        """Test validation when no ethernet interface is up."""
        with (
            patch(
                "spark_pulse.tools.discovery.detect_network_interfaces"
            ) as mock_ifaces,
            patch("spark_pulse.tools.discovery.detect_local_ip") as mock_ip,
            patch("spark_pulse.tools.discovery.detect_infiniband_devices") as mock_ib,
            patch("spark_pulse.tools.discovery._port_available") as mock_port,
        ):
            mock_ifaces.return_value = [
                NetworkInterface(
                    name="lo", ip="127.0.0.1", mtu=65536, is_up=True, type="loopback"
                ),
            ]
            mock_ip.return_value = None
            mock_ib.return_value = []
            mock_port.return_value = True

            result = validate_network()
            assert result.healthy is False
            assert any("ethernet" in e.lower() for e in result.errors)

    def test_port_in_use(self):
        """Test validation when common ports are in use."""
        with (
            patch(
                "spark_pulse.tools.discovery.detect_network_interfaces"
            ) as mock_ifaces,
            patch("spark_pulse.tools.discovery.detect_local_ip") as mock_ip,
            patch("spark_pulse.tools.discovery.detect_infiniband_devices") as mock_ib,
            patch("spark_pulse.tools.discovery._port_available") as mock_port,
        ):
            mock_ifaces.return_value = [
                NetworkInterface(
                    name="eth0",
                    ip="192.168.1.100",
                    mtu=1500,
                    is_up=True,
                    type="ethernet",
                ),
            ]
            mock_ip.return_value = "192.168.1.100"
            mock_ib.return_value = []
            mock_port.side_effect = lambda port: port not in [29500, 29501]

            result = validate_network()
            assert result.healthy is True  # port conflicts are warnings, not errors
            assert any("29500" in w or "29501" in w for w in result.warnings)


class TestMockDiscoveryProvider:
    """Test mock discovery provider scenarios."""

    def test_single_gpu_scenario(self):
        """Test single_gpu scenario: eth0 only, no IB."""
        from spark_pulse.mock.discovery import MockDiscoveryProvider

        provider = MockDiscoveryProvider(scenario="single_gpu")
        result = provider.run_discovery()

        assert result.local_ip == "192.168.1.100"
        assert result.ethernet_if == "eth0"
        assert result.infiniband_present is False
        assert len(result.infiniband_devices) == 0
        assert result.nccl_defaults is not None
        assert result.nccl_defaults.ib_disable is True

    def test_dgx_scenario(self):
        """Test dgx scenario: eth0 + 4x mlx5 HCAs."""
        from spark_pulse.mock.discovery import MockDiscoveryProvider

        provider = MockDiscoveryProvider(scenario="dgx")
        result = provider.run_discovery()

        assert result.local_ip == "10.0.0.10"
        assert result.ethernet_if == "eth0"
        assert result.infiniband_present is True
        assert len(result.infiniband_devices) == 4
        assert result.nccl_defaults is not None
        assert result.nccl_defaults.ib_hca == "mlx5_0"
        assert result.nccl_defaults.ib_disable is False

    def test_multi_node_scenario(self):
        """Test multi_node scenario: eth0 + IB + multiple IPs."""
        from spark_pulse.mock.discovery import MockDiscoveryProvider

        provider = MockDiscoveryProvider(scenario="multi_node")
        result = provider.run_discovery()

        assert result.local_ip == "10.0.0.10"
        assert result.infiniband_present is True
        assert len(result.infiniband_devices) == 4

    def test_broken_ib_scenario(self):
        """Test broken_ib scenario: IB installed but down."""
        from spark_pulse.mock.discovery import MockDiscoveryProvider

        provider = MockDiscoveryProvider(scenario="broken_ib")
        result = provider.run_discovery()

        assert result.infiniband_present is True
        assert result.nccl_defaults is not None
        assert result.nccl_defaults.ib_disable is True

        validation = provider.get_validation()
        assert validation.healthy is False
        assert len(validation.warnings) > 0

    def test_set_mock_scenario(self):
        """Test set_mock_scenario changes the active scenario."""
        from spark_pulse.mock.discovery import set_mock_scenario, reset_mock_scenario

        set_mock_scenario("dgx")
        from spark_pulse.mock.discovery import _get_default_provider

        assert _get_default_provider().scenario == "dgx"

        reset_mock_scenario()
        assert _get_default_provider().scenario == "single_gpu"


class TestModuleLevelConvenience:
    """Test module-level convenience functions."""

    def test_get_network_interfaces(self):
        """Test get_network_interfaces convenience function."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        interfaces = discovery.get_network_interfaces()
        assert isinstance(interfaces, list)

    def test_get_local_ip(self):
        """Test get_local_ip convenience function."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        ip = discovery.get_local_ip()
        assert ip is None or isinstance(ip, str)

    def test_get_infiniband_devices(self):
        """Test get_infiniband_devices convenience function."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        devices = discovery.get_infiniband_devices()
        assert isinstance(devices, list)

    def test_get_nccl_defaults(self):
        """Test get_nccl_defaults convenience function."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        nccl = discovery.get_nccl_defaults()
        assert nccl is None or isinstance(nccl, NCCLConfig)

    def test_check_network_health(self):
        """Test check_network_health convenience function."""
        if not is_simulation():
            pytest.skip("Running in production mode")

        result = discovery.check_network_health()
        assert isinstance(result, ValidationResult)


# ── The CX7 fabric ───────────────────────────────────────────────────────────
#
# ``spark-vllm-docker``'s ``autodiscover.sh`` ``detect_interfaces`` (lines
# 56-196) is the only sanctioned description of how a DGX Spark's ConnectX
# ports are read, and these are its rules one at a time. The sample output is
# verbatim from that repository's ``docs/NETWORKING.md`` lines 22-27 and the
# addresses from its netplan profiles at lines 95-131 and 162-253.

from spark_pulse.tools.discovery import (  # noqa: E402
    FABRIC_DIRECT,
    FABRIC_MESH,
    MESH_NCCL_ENV,
    build_fabric_config,
    detect_fabric,
    fabric_from_output,
    parse_ibdev2netdev,
)

ONE_CABLE = (
    "rocep1s0f0 port 1 ==> enp1s0f0np0 (Down)\n"
    "rocep1s0f1 port 1 ==> enp1s0f1np1 (Up)\n"
    "roceP2p1s0f0 port 1 ==> enP2p1s0f0np0 (Down)\n"
    "roceP2p1s0f1 port 1 ==> enP2p1s0f1np1 (Up)"
)

ONE_CABLE_ADDRS = (
    "1: lo    inet 127.0.0.1/8 scope host lo\n"
    "2: enp1s0f1np1    inet 192.168.177.11/24 scope global enp1s0f1np1\n"
    "3: enP2p1s0f1np1    inet 192.168.178.11/24 scope global enP2p1s0f1np1"
)

MESH = (
    "rocep1s0f0 port 1 ==> enp1s0f0np0 (Up)\n"
    "rocep1s0f1 port 1 ==> enp1s0f1np1 (Up)\n"
    "roceP2p1s0f0 port 1 ==> enP2p1s0f0np0 (Up)\n"
    "roceP2p1s0f1 port 1 ==> enP2p1s0f1np1 (Up)"
)

MESH_ADDRS = (
    "2: enp1s0f0np0    inet 192.168.177.11/24 scope global enp1s0f0np0\n"
    "3: enP2p1s0f0np0    inet 192.168.178.11/24 scope global enP2p1s0f0np0\n"
    "4: enp1s0f1np1    inet 192.168.187.11/24 scope global enp1s0f1np1\n"
    "5: enP2p1s0f1np1    inet 192.168.188.11/24 scope global enP2p1s0f1np1\n"
    "6: enP7s7    inet 10.0.0.11/24 scope global enP7s7"
)


def fabric(ibdev: str, addrs: str):
    return fabric_from_output(ibdev + "\n== addr\n" + addrs)


class TestParsing:
    def test_a_port_line_yields_its_device_netdev_and_state(self):
        ports = parse_ibdev2netdev(ONE_CABLE)
        assert [p.hca for p in ports] == [
            "rocep1s0f0",
            "rocep1s0f1",
            "roceP2p1s0f0",
            "roceP2p1s0f1",
        ]
        assert [p.netdev for p in ports] == [
            "enp1s0f0np0",
            "enp1s0f1np1",
            "enP2p1s0f0np0",
            "enP2p1s0f1np1",
        ]
        assert [p.is_up for p in ports] == [False, True, False, True]
        assert {p.port for p in ports} == {1}

    def test_noise_is_ignored_rather_than_guessed_at(self):
        assert parse_ibdev2netdev("bash: ibdev2netdev: not found") == []
        assert parse_ibdev2netdev("") == []


class TestOneCable:
    """Two ports up: the pair, or any node behind a QSFP switch."""

    def test_both_roce_twins_are_named(self):
        """NETWORKING.md line 38, the whole reason this section exists.

        Each QSFP port is limited to a PCIe 5.0 x4 link and is presented as
        two RoCE devices; NCCL reaches full bandwidth only when told both.
        """
        config = fabric(ONE_CABLE, ONE_CABLE_ADDRS)
        assert config.mode == FABRIC_DIRECT
        assert config.ib_hca == ("rocep1s0f1", "roceP2p1s0f1")
        assert config.ib_hca_value == "rocep1s0f1,roceP2p1s0f1"

    def test_the_addressed_twin_is_the_management_link(self):
        """autodiscover.sh lines 132-142 prefer the name without a capital P."""
        config = fabric(ONE_CABLE, ONE_CABLE_ADDRS)
        assert config.ethernet == "enp1s0f1np1"

    def test_a_single_cable_gets_no_mesh_settings(self):
        config = fabric(ONE_CABLE, ONE_CABLE_ADDRS)
        assert config.nccl_env == {}
        assert not config.is_mesh
        assert config.ok

    def test_the_twins_of_a_port_are_recoverable_from_either_one(self):
        config = fabric(ONE_CABLE, ONE_CABLE_ADDRS)
        both = ("rocep1s0f1", "roceP2p1s0f1")
        assert config.twins_of("rocep1s0f1") == both
        assert config.twins_of("roceP2p1s0f1") == both
        assert config.twins_of("mlx5_0") == ()


class TestMesh:
    """Four ports up: the switchless ring, NETWORKING.md lines 45-82."""

    def test_all_four_roce_devices_are_named(self):
        config = fabric(MESH, MESH_ADDRS)
        assert config.mode == FABRIC_MESH
        assert set(config.ib_hca) == {
            "rocep1s0f0",
            "rocep1s0f1",
            "roceP2p1s0f0",
            "roceP2p1s0f1",
        }

    def test_coordination_moves_to_the_10g_link(self):
        """NETWORKING.md line 434: every CX7 port carries the ring."""
        assert fabric(MESH, MESH_ADDRS).ethernet == "enP7s7"

    def test_the_three_mesh_nccl_settings_are_emitted(self):
        assert fabric(MESH, MESH_ADDRS).nccl_env == MESH_NCCL_ENV
        assert MESH_NCCL_ENV == {
            "NCCL_NET_PLUGIN": "none",
            "NCCL_IB_SUBNET_AWARE_ROUTING": "1",
            "NCCL_IB_MERGE_NICS": "0",
        }

    def test_wireless_coordination_is_accepted_with_a_warning(self):
        """autodiscover.sh lines 176-180 take wlP9s9 and say so."""
        addrs = MESH_ADDRS.replace("enP7s7", "wlP9s9")
        config = fabric(MESH, addrs)
        assert config.ethernet == "wlP9s9"
        assert config.ok
        assert any("wireless" in note for note in config.warnings)

    def test_a_mesh_with_no_management_link_is_refused(self):
        """autodiscover.sh line 181."""
        addrs = "\n".join(
            line for line in MESH_ADDRS.splitlines() if "enP7s7" not in line
        )
        config = fabric(MESH, addrs)
        assert not config.ok
        assert any("enP7s7 or wlP9s9" in problem for problem in config.errors)


class TestFabricRefusals:
    """Every condition ``detect_interfaces`` returns 1 on."""

    def test_an_up_link_without_an_address_is_refused(self):
        """autodiscover.sh lines 94-101."""
        addrs = "3: enP2p1s0f1np1    inet 192.168.178.11/24 scope global x"
        config = fabric(ONE_CABLE, addrs)
        assert not config.ok
        assert any("enp1s0f1np1" in problem for problem in config.errors)

    def test_the_capital_p_twin_may_be_unaddressed(self):
        """Only one twin needs an IP — NETWORKING.md line 37."""
        addrs = "2: enp1s0f1np1    inet 192.168.177.11/24 scope global enp1s0f1np1"
        config = fabric(ONE_CABLE, addrs)
        assert config.ok
        assert config.ethernet == "enp1s0f1np1"

    def test_two_links_on_one_subnet_are_refused(self):
        """autodiscover.sh lines 103-117; NETWORKING.md line 133 in bold."""
        addrs = (
            "2: enp1s0f1np1    inet 192.168.177.11/24 scope global enp1s0f1np1\n"
            "3: enP2p1s0f1np1    inet 192.168.177.12/24 scope global enP2p1s0f1np1"
        )
        config = fabric(ONE_CABLE, addrs)
        assert not config.ok
        assert any("192.168.177.0/24" in problem for problem in config.errors)

    def test_a_port_count_that_is_neither_two_nor_four_is_refused(self):
        """autodiscover.sh line 193 names the number it found."""
        three = "\n".join(MESH.splitlines()[:3])
        config = fabric(three, MESH_ADDRS)
        assert not config.ok
        assert any("(3)" in problem for problem in config.errors)

    def test_no_ports_at_all_is_a_warning_and_not_a_mode(self):
        """A developer machine has no fabric; that is not an error here."""
        config = build_fabric_config([], {})
        assert config.mode == ""
        assert config.errors == ()
        assert config.warnings


class TestNcclDefaultsUseTheFabric:
    def test_every_active_hca_is_named_not_the_first(self):
        """The pre-fabric code took ``devices[0].hca`` and stopped."""
        result = DiscoveryResult(
            local_ip="10.0.0.1",
            ethernet_if="eth0",
            infiniband_present=True,
            infiniband_devices=[
                InfinibandDevice("mlx5_0", [1], ["ib0"], "ACTIVE"),
                InfinibandDevice("mlx5_1", [1], ["ib1"], "ACTIVE"),
            ],
            interfaces=[
                NetworkInterface(
                    name="eth0", ip="10.0.0.1", mtu=1500, is_up=True, type="ethernet"
                )
            ],
            nccl_defaults=None,
        )
        assert build_nccl_defaults(result).ib_hca == "mlx5_0,mlx5_1"

    def test_a_discovered_fabric_wins_over_the_interface_scan(self):
        """The scan cannot see RoCE devices: they are not netdevs."""
        result = DiscoveryResult(
            local_ip="10.0.0.1",
            ethernet_if="eth0",
            infiniband_present=True,
            infiniband_devices=[],
            interfaces=[
                NetworkInterface(
                    name="eth0", ip="10.0.0.1", mtu=1500, is_up=True, type="ethernet"
                )
            ],
            nccl_defaults=None,
            fabric=fabric(ONE_CABLE, ONE_CABLE_ADDRS),
        )
        nccl = build_nccl_defaults(result)
        assert nccl.socket_ifname == "enp1s0f1np1"
        assert nccl.ib_hca == "rocep1s0f1,roceP2p1s0f1"
        assert nccl.ib_disable is False


class TestLocalFabricDetection:
    """The real detector, which is what runs outside simulation."""

    def test_detection_degrades_to_an_empty_config_when_the_tool_is_absent(self):
        with patch(
            "spark_pulse.tools.discovery.subprocess.run", side_effect=FileNotFoundError
        ):
            config = detect_fabric()
        assert config.mode == ""
        assert config.ports == ()

    def test_a_workstation_scenario_simply_has_no_fabric(self):
        discovery.set_mock_scenario("single_gpu")
        try:
            config = discovery.detect_fabric()
        finally:
            discovery.reset_mock_scenario()
        assert config.mode == ""
        assert config.errors == ()

    def test_the_simulated_spark_is_the_one_cable_shape(self):
        """Simulation answers with NETWORKING.md's own sample output."""
        discovery.set_mock_scenario("dgx")
        try:
            config = discovery.detect_fabric()
        finally:
            discovery.reset_mock_scenario()
        assert config.mode == FABRIC_DIRECT
        assert config.ib_hca_value == "rocep1s0f1,roceP2p1s0f1"
