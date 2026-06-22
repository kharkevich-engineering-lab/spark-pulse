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
