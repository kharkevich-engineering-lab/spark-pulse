"""The caches, on every node, through every node's own agent.

`tools/cache.py` used to walk `~/.cache` in this process and delete what it
found there, which made the Library's Caches section a report about the control
node presented as a report about the cluster. The walk and the delete are now
`ScanCache`/`CleanCache` on the node that owns the bytes (`agent/src/executor/
cache.rs`, tested there against real directories), and what is left here is the
part that is genuinely the control plane's: the cache *definitions*, asking
every node, and the two rules that protect the model cache.

So these tests are about the seam rather than about a filesystem. Each drives
the module through a fake resolver — the same shape `node_stats` tests use —
because the question is which node was asked what, and that is invisible to a
test that asserts on bytes.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from spark_pulse import tools as _tools_pkg
from spark_pulse.agent import agent_pb2 as pb

# The tools package attribute is whichever twin the simulation switch
# installed; sys.modules is what holds the real submodule. Remember the
# switch's choice, import, put it back — the idiom conftest.py uses.
_switched = _tools_pkg.cache
import spark_pulse.tools.cache  # noqa: E402,F401

cache = sys.modules["spark_pulse.tools.cache"]
_tools_pkg.cache = _switched


# ── A fleet that answers ─────────────────────────────────────────────────────


class FakeNodeService:
    """One node's agent, as far as this module can tell."""

    def __init__(self, sizes: dict[str, int] | None = None, home: str = "/home/spark"):
        self.home = home
        self.sizes = sizes if sizes is not None else {}
        self.scanned: list[list[str]] = []
        self.cleaned: list[tuple[list[str], bool]] = []

    def _expand(self, path: str) -> str:
        return path.replace("~", self.home, 1)

    def scan_cache(self, paths):
        paths = list(paths)
        self.scanned.append(paths)
        scan = pb.CacheScan()
        for path in paths:
            scan.dirs.append(
                pb.CacheDir(
                    path=self._expand(path),
                    exists=path in self.sizes,
                    bytes=self.sizes.get(path, 0),
                    files=3 if path in self.sizes else 0,
                )
            )
        return scan

    def clean_cache(self, paths, include_hub=False):
        paths = list(paths)
        self.cleaned.append((paths, include_hub))
        cleaned = pb.CacheClean()
        for path in paths:
            cleaned.results.append(
                pb.CacheDirResult(
                    path=self._expand(path),
                    removed=path in self.sizes,
                    freed_bytes=self.sizes.get(path, 0),
                )
            )
        return cleaned


class Unreachable:
    """A node whose agent is not there."""

    def __init__(self, message: str = "no agent for 10.0.0.11"):
        self.message = message

    def scan_cache(self, paths):
        raise RuntimeError(self.message)

    def clean_cache(self, paths, include_hub=False):
        raise RuntimeError(self.message)


class Record:
    def __init__(self, id, name, address, is_control_plane=False):
        self.id = id
        self.name = name
        self.address = address
        self.is_control_plane = is_control_plane
        self.ssh_user = ""


@pytest.fixture
def fleet(monkeypatch):
    """Two nodes with their own services, resolved by address."""
    everything = {entry["path"]: 100 for entry in cache.get_cache_dirs()}
    services = {
        "192.168.1.100": FakeNodeService(dict(everything)),
        "10.0.0.11": FakeNodeService({"~/.triton": 42}),
    }
    records = [
        Record("control-1", "spark-01", "192.168.1.100", is_control_plane=True),
        Record("peer-1", "spark-02", "10.0.0.11"),
    ]
    monkeypatch.setattr(cache, "_registry_nodes", lambda: list(records))
    return {"services": services, "records": records}


@pytest.fixture
def resolve(fleet):
    """A resolver that hands each node its own service, by address."""

    def _resolve(node):
        return fleet["services"][node.address]

    return _resolve


# ── The definitions ──────────────────────────────────────────────────────────


class TestTheDefinitions:
    """Name, path and description stay here; the expansion happens on the node."""

    def test_the_listed_caches_are_the_engine_runtime_ones(self):
        names = {entry["name"] for entry in cache.get_cache_dirs()}

        assert names == {
            "HF Model Cache",
            "vLLM Cache",
            "FlashInfer Cache",
            "Triton Cache",
        }

    def test_every_path_is_relative_to_a_node_s_own_home(self):
        # Not `os.path.expanduser`: that answers for the control plane, and
        # sending the answer to a peer names a directory on the wrong machine.
        paths = [entry["path"] for entry in cache.get_cache_dirs()]

        assert all(path.startswith("~/") for path in paths), paths

    def test_the_hub_cache_is_named_because_two_rules_turn_on_it(self):
        assert cache.HUB_CACHE_NAME in {e["name"] for e in cache.get_cache_dirs()}


# ── Every node ───────────────────────────────────────────────────────────────


class TestGetCacheStatus:
    def test_every_registered_node_gets_a_section_control_plane_first(
        self, fleet, resolve
    ):
        status = cache.get_cache_status(resolve)

        assert [node["node_id"] for node in status["nodes"]] == [
            "control-1",
            "peer-1",
        ]
        assert status["nodes"][0]["is_control_plane"] is True
        assert status["nodes"][1]["name"] == "spark-02"

    def test_each_node_is_asked_for_its_own_caches(self, fleet, resolve):
        cache.get_cache_status(resolve)

        for service in fleet["services"].values():
            assert service.scanned == [[e["path"] for e in cache.get_cache_dirs()]]

    def test_the_node_s_resolved_path_is_what_comes_back(self, fleet, resolve):
        status = cache.get_cache_status(resolve)

        paths = [entry["path"] for entry in status["nodes"][0]["dirs"]]
        assert all(path.startswith("/home/spark/") for path in paths), paths

    def test_the_definition_s_name_and_description_stay_attached(self, fleet, resolve):
        status = cache.get_cache_status(resolve)

        first = status["nodes"][0]["dirs"][0]
        assert first["name"] == "HF Model Cache"
        assert first["description"] == "Downloaded HuggingFace models"

    def test_a_total_is_the_sum_of_that_node_s_own_caches(self, fleet, resolve):
        status = cache.get_cache_status(resolve)

        assert status["nodes"][0]["total_bytes"] == 400
        assert status["nodes"][1]["total_bytes"] == 42

    def test_a_node_that_holds_nothing_says_the_directory_is_absent(
        self, fleet, resolve
    ):
        status = cache.get_cache_status(resolve)

        peer = {entry["name"]: entry for entry in status["nodes"][1]["dirs"]}
        assert peer["Triton Cache"]["exists"] is True
        assert peer["vLLM Cache"]["exists"] is False
        assert peer["vLLM Cache"]["size_bytes"] == 0


class TestANodeThatCannotBeAsked:
    """Unknown is not empty. The section stays, and it says why."""

    def test_the_section_survives_with_the_reason(self, fleet, monkeypatch):
        fleet["services"]["10.0.0.11"] = Unreachable()

        status = cache.get_cache_status(lambda node: fleet["services"][node.address])

        peer = status["nodes"][1]
        assert peer["reachable"] is False
        assert peer["reason"] == "no agent for 10.0.0.11"
        assert peer["dirs"] == []
        assert peer["total_bytes"] == 0

    def test_one_silent_node_does_not_cost_the_others_their_numbers(self, fleet):
        fleet["services"]["10.0.0.11"] = Unreachable()

        status = cache.get_cache_status(lambda node: fleet["services"][node.address])

        assert status["nodes"][0]["reachable"] is True
        assert status["nodes"][0]["total_bytes"] == 400

    def test_an_agent_too_old_for_the_operation_is_told_to_update(self, fleet):
        # A pre-1.29 agent answers "command carries no op", which is true and
        # unhelpful. The remedy is an agent update, so that is what it says —
        # the same reading `AgentHostProbe` gives a missing `RunHostProbe`.
        fleet["services"]["10.0.0.11"] = Unreachable("command carries no op")

        status = cache.get_cache_status(lambda node: fleet["services"][node.address])

        assert "too old" in status["nodes"][1]["reason"]
        assert "update" in status["nodes"][1]["reason"]


class TestWithNoRegistry:
    def test_a_control_plane_with_no_registry_still_answers_for_itself(
        self, monkeypatch
    ):
        monkeypatch.setattr(cache, "_registry_nodes", lambda: [])
        service = FakeNodeService(
            {entry["path"]: 5 for entry in cache.get_cache_dirs()}
        )

        status = cache.get_cache_status(lambda node: service)

        assert len(status["nodes"]) == 1
        assert status["nodes"][0]["is_control_plane"] is True
        assert status["nodes"][0]["total_bytes"] == 20


# ── Emptying ─────────────────────────────────────────────────────────────────


class TestCleanCache:
    def test_one_named_cache_is_emptied_on_the_node_that_was_named(
        self, fleet, resolve
    ):
        out = cache.clean_cache("peer-1", "Triton Cache", resolve)

        assert out["reachable"] is True
        assert out["results"] == [
            {
                "name": "Triton Cache",
                "path": "/home/spark/.triton",
                "removed": True,
                "freed_bytes": 42,
                "error": None,
            }
        ]
        # And nothing was asked of the other node.
        assert fleet["services"]["192.168.1.100"].cleaned == []

    def test_naming_the_hub_cache_is_what_unlocks_it(self, fleet, resolve):
        cache.clean_cache("control-1", cache.HUB_CACHE_NAME, resolve)

        paths, include_hub = fleet["services"]["192.168.1.100"].cleaned[0]
        assert paths == ["~/.cache/huggingface/hub"]
        assert include_hub is True

    def test_any_other_cache_leaves_the_flag_alone(self, fleet, resolve):
        cache.clean_cache("control-1", "vLLM Cache", resolve)

        _paths, include_hub = fleet["services"]["192.168.1.100"].cleaned[0]
        assert include_hub is False

    def test_a_node_may_be_named_by_its_address(self, fleet, resolve):
        out = cache.clean_cache("10.0.0.11", "Triton Cache", resolve)

        assert out["reachable"] is True

    def test_an_unknown_cache_deletes_nothing_and_says_so(self, fleet, resolve):
        out = cache.clean_cache("control-1", "Bitcoin", resolve)

        assert out["results"][0]["error"] == "Unknown cache: Bitcoin"
        assert fleet["services"]["192.168.1.100"].cleaned == []

    def test_an_unknown_node_is_unreachable_rather_than_the_control_node(
        self, fleet, resolve
    ):
        # The old API had no node at all, so "which machine" had one answer.
        # A name that resolves to nothing must not quietly become this one.
        out = cache.clean_cache("spark-99", "Triton Cache", resolve)

        assert out["reachable"] is False
        assert "no such node" in out["reason"]
        assert fleet["services"]["192.168.1.100"].cleaned == []

    def test_a_node_that_cannot_be_asked_says_so(self, fleet):
        fleet["services"]["10.0.0.11"] = Unreachable()

        out = cache.clean_cache(
            "peer-1", "Triton Cache", lambda node: fleet["services"][node.address]
        )

        assert out["reachable"] is False
        assert out["results"] == []


class TestCleanAll:
    def test_it_sweeps_every_cache_but_the_models(self, fleet, resolve):
        out = cache.clean_all("control-1", resolve)

        paths, include_hub = fleet["services"]["192.168.1.100"].cleaned[0]
        assert "~/.cache/huggingface/hub" not in paths
        assert len(paths) == 3
        assert include_hub is False
        assert [entry["name"] for entry in out["results"]] == [
            "vLLM Cache",
            "FlashInfer Cache",
            "Triton Cache",
        ]

    def test_it_is_one_node_at_a_time(self, fleet, resolve):
        cache.clean_all("peer-1", resolve)

        assert fleet["services"]["192.168.1.100"].cleaned == []
        assert fleet["services"]["10.0.0.11"].cleaned != []

    def test_a_control_plane_with_no_registry_can_still_be_swept(self, monkeypatch):
        monkeypatch.setattr(cache, "_registry_nodes", lambda: [])
        service = FakeNodeService({"~/.triton": 9})

        out = cache.clean_all("control", lambda node: service)

        assert out["reachable"] is True
        assert service.cleaned[0][1] is False


# ── The simulation twin ──────────────────────────────────────────────────────


class TestMockCache:
    """``mock/cache.py`` is the real aggregator over a simulated fleet."""

    @pytest.fixture(autouse=True)
    def fleet(self):
        """A fresh simulated fleet, before *and* after.

        These tests empty caches on a simulated node, and that node's table is
        module state shared with every other test in the session — a clean that
        leaks is a later test finding a cache it expected bytes in already at
        zero, which is exactly the failure mode the per-node table exists to
        make visible.
        """
        from spark_pulse.mock import docker as mock_docker
        from spark_pulse.mock import node_service as mock_node_service

        def _restore() -> None:
            mock_node_service.reset()
            mock_docker._get_service().caches = dict(mock_docker.SIMULATED_CACHES)

        _restore()
        yield
        _restore()

    def test_it_exposes_every_public_name_the_real_module_does(self):
        mock_cache = importlib.import_module("spark_pulse.mock.cache")

        expected = {
            name
            for name, value in vars(cache).items()
            if not name.startswith("_")
            and getattr(value, "__module__", None) == cache.__name__
        }

        assert expected == {
            "get_cache_dirs",
            "get_cache_status",
            "for_node",
            "clean_cache",
            "clean_all",
        }
        assert {n for n in expected if not hasattr(mock_cache, n)} == set()

    def test_it_is_the_real_implementation_rather_than_a_second_one(self):
        # A parallel copy cannot catch a bug in the code it stands in for; the
        # only thing simulation swaps is the transport underneath.
        mock_cache = importlib.import_module("spark_pulse.mock.cache")

        assert mock_cache.get_cache_status is cache.get_cache_status
        assert mock_cache.clean_all is cache.clean_all

    def test_the_simulated_fleet_answers_the_whole_shape(self):
        mock_cache = importlib.import_module("spark_pulse.mock.cache")
        from spark_pulse.mock import node_service as mock_node_service

        mock_node_service.reset()
        status = mock_cache.get_cache_status()

        assert len(status["nodes"]) >= 2
        for node in status["nodes"]:
            assert set(node) >= {
                "node_id",
                "name",
                "address",
                "is_control_plane",
                "reachable",
                "reason",
                "total_bytes",
                "dirs",
            }
            assert node["reachable"] is True
            assert node["total_bytes"] > 0
            assert [entry["name"] for entry in node["dirs"]] == [
                entry["name"] for entry in mock_cache.get_cache_dirs()
            ]
            assert all("~" not in entry["path"] for entry in node["dirs"])

    def test_two_simulated_nodes_hold_two_sets_of_caches(self):
        mock_cache = importlib.import_module("spark_pulse.mock.cache")
        from spark_pulse.mock import node_service as mock_node_service

        mock_node_service.reset()
        before = mock_cache.get_cache_status()["nodes"]
        peer = next(node for node in before if not node["is_control_plane"])

        mock_cache.clean_all(peer["node_id"])
        after = mock_cache.get_cache_status()["nodes"]

        control_before = next(n for n in before if n["is_control_plane"])
        control_after = next(n for n in after if n["is_control_plane"])
        peer_after = next(n for n in after if not n["is_control_plane"])
        assert control_after["total_bytes"] == control_before["total_bytes"]
        assert peer_after["total_bytes"] < peer["total_bytes"]

    def test_the_simulated_node_refuses_the_hub_without_leave(self):
        """The refusal is the agent's, so simulation has to be able to make it."""
        from spark_pulse.mock import docker as mock_docker

        service = mock_docker.MockDockerService()
        refused = service.clean_cache(["~/.cache/huggingface/hub"], include_hub=False)

        assert refused.results[0].removed is False
        assert "Models section" in refused.results[0].error

        allowed = service.clean_cache(["~/.cache/huggingface/hub"], include_hub=True)

        assert allowed.results[0].removed is True
