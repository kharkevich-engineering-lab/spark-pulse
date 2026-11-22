"""The Kubernetes backend renders a run, and applies nothing (#164)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from spark_pulse import tools
from spark_pulse.app import create_app
from spark_pulse.mock import node_registry as mock_registry
from spark_pulse.mock import node_service as mock_node_service
from spark_pulse.tools.backends import RunBackend, backend_for
from spark_pulse.tools.backends.kubernetes import (
    HF_CACHE_HOSTPATH,
    KubernetesBackend,
    _render_on_pods,
)
from spark_pulse.tools.runtime_errors import NativeRuntimeError

RECIPE = "bundled/qwen2.5-0.5b-instruct"
TOKEN = "hf_kubernetes_token_must_never_appear"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    mock_registry.reset()
    mock_node_service.reset()
    yield
    mock_registry.reset()
    mock_node_service.reset()


def by_kind(plan: dict, kind: str) -> dict:
    return next(m for m in plan["manifests"] if m["kind"] == kind)


def codes(plan: dict) -> set[str]:
    return {f["code"] for f in plan["findings"]}


def solo() -> dict:
    return KubernetesBackend().plan(RECIPE).to_dict()


def pair() -> dict:
    nodes = [n.address for n in mock_registry.list_nodes()]
    return (
        KubernetesBackend()
        .plan(RECIPE, nodes=nodes, solo=False, params={"tensor_parallel": 2})
        .to_dict()
    )


class TestOneRank:
    def test_it_is_a_deployment_a_service_and_the_cache_claim(self):
        plan = solo()
        kinds = [m["kind"] for m in plan["manifests"]]
        assert kinds == ["PersistentVolumeClaim", "Deployment", "Service"]
        assert plan["applied"] is False
        assert plan["backend"] == "kubernetes"

    def test_the_container_runs_the_engines_own_rendering(self):
        plan = solo()
        pod = by_kind(plan, "Deployment")["spec"]["template"]["spec"]
        engine = pod["containers"][0]
        assert engine["image"] == plan["image"]
        assert engine["command"] == ["/bin/bash", "-c"]
        assert engine["args"][0].startswith("vllm serve Qwen/Qwen2.5-0.5B-Instruct")
        assert f"--port {plan['port']}" in engine["args"][0]
        assert engine["resources"]["limits"]["nvidia.com/gpu"] == 1
        assert engine["readinessProbe"]["httpGet"]["path"] == "/v1/models"

    def test_the_token_is_a_secret_reference_never_its_value(self):
        plan = solo()
        assert TOKEN not in plan["yaml"]
        env = by_kind(plan, "Deployment")["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
        token = next(e for e in env if e["name"] == "HF_TOKEN")
        assert token["valueFrom"]["secretKeyRef"]["name"] == "spark-pulse-hf-token"

    def test_the_cache_is_a_claim_by_default_and_a_host_path_on_request(self):
        claim = by_kind(solo(), "Deployment")["spec"]["template"]["spec"]["volumes"][0]
        assert claim["persistentVolumeClaim"]["claimName"] == "spark-pulse-hf-cache"

        on_host = KubernetesBackend().plan(RECIPE, hf_cache=HF_CACHE_HOSTPATH).to_dict()
        assert "PersistentVolumeClaim" not in [m["kind"] for m in on_host["manifests"]]
        volume = by_kind(on_host, "Deployment")["spec"]["template"]["spec"]["volumes"][
            0
        ]
        assert volume["hostPath"]["path"] == "/var/lib/spark-pulse/huggingface"

    def test_an_unknown_cache_kind_is_refused(self):
        with pytest.raises(NativeRuntimeError, match="hf_cache"):
            KubernetesBackend().plan(RECIPE, hf_cache="nfs")

    def test_the_yaml_is_every_manifest_without_anchors(self):
        plan = solo()
        assert list(yaml.safe_load_all(plan["yaml"])) == plan["manifests"]
        assert "&id" not in plan["yaml"]

    def test_names_are_dns_labels(self):
        name = by_kind(solo(), "Deployment")["metadata"]["name"]
        assert len(name) <= 63 and name == name.lower() and "_" not in name

    def test_what_the_intent_could_not_say_is_reported(self):
        assert {"gpus-per-rank", "model-file-unknown", "engine-user"} <= codes(solo())

    def test_rendering_asks_no_node_anything(self):
        solo()
        assert mock_node_service.calls == []


class TestSeveralRanks:
    def test_a_rendezvous_engine_is_a_leader_worker_set(self):
        lws = by_kind(pair(), "LeaderWorkerSet")
        assert lws["apiVersion"] == "leaderworkerset.x-k8s.io/v1"
        assert lws["spec"]["leaderWorkerTemplate"]["size"] == 2

    def test_the_leader_is_rank_zero_at_the_address_lws_injects(self):
        template = by_kind(pair(), "LeaderWorkerSet")["spec"]["leaderWorkerTemplate"]
        args = template["leaderTemplate"]["spec"]["containers"][0]["args"][0]
        assert "--node-rank 0" in args
        assert "--master-addr $(LWS_LEADER_ADDRESS)" in args

    def test_each_worker_runs_its_own_ranks_command(self):
        template = by_kind(pair(), "LeaderWorkerSet")["spec"]["leaderWorkerTemplate"]
        worker = template["workerTemplate"]["spec"]["containers"][0]
        args = worker["args"][0]
        assert args.startswith('case "$LWS_WORKER_INDEX" in')
        assert "1) exec vllm serve" in args and "--node-rank 1" in args
        env = {e["name"]: e for e in worker["env"]}
        assert env["VLLM_HOST_IP"]["value"] == "$(POD_IP)"
        # A worker loads nothing, so it is handed no token.
        assert "HF_TOKEN" not in env
        assert "readinessProbe" not in worker

    def test_the_service_selects_the_leader(self):
        selector = by_kind(pair(), "Service")["spec"]["selector"]
        assert selector["leaderworkerset.sigs.k8s.io/worker-index"] == "0"

    def test_replacing_the_hosts_is_reported_as_a_defect_of_the_intent(self):
        assert {"topology-names-hosts", "host-ip-is-a-name"} <= codes(pair())

    def test_an_rpc_engine_is_refused_not_guessed(self):
        intent = SimpleNamespace(
            rpc_port=50052, engine_name="llama-cpp", variant="prism"
        )
        with pytest.raises(NativeRuntimeError, match="RPC"):
            _render_on_pods(intent, 8080, [])


class TestTheBackend:
    def test_it_is_a_run_backend(self):
        assert isinstance(KubernetesBackend(), RunBackend)

    def test_it_is_never_the_one_chosen(self):
        assert backend_for({"id": "x"}).name == "docker"

    @pytest.mark.parametrize(
        ("call", "args"),
        [
            ("start", (RECIPE, "n", {})),
            ("status", ("x",)),
            ("stop", ("x",)),
            ("delete", ("x",)),
            ("logs", ("x",)),
        ],
    )
    def test_its_lifecycle_refuses(self, call, args):
        with pytest.raises(NativeRuntimeError, match="renders manifests only"):
            getattr(KubernetesBackend(), call)(*args)

    def test_it_owns_no_runs_and_checks_no_node(self):
        backend = KubernetesBackend()
        assert backend.list_runs() == []
        assert backend.diagnose("node").findings == []

    def test_realise_takes_the_intent_the_docker_backend_takes(self):
        from spark_pulse.engines import get_registry
        from spark_pulse.tools.intent import resolve_intent

        intent = resolve_intent(
            RECIPE, registry=get_registry(), allow_missing_model=True
        )
        plan = KubernetesBackend().realise(intent).to_dict()
        assert plan["intent_hash"] == intent.intent_hash()
        annotations = by_kind(plan, "Deployment")["metadata"]["annotations"]
        assert annotations["spark-pulse.intent-hash"] == intent.intent_hash()


class TestTheEndpoint:
    def test_it_answers_the_manifests(self):
        client = TestClient(create_app())
        resp = client.post(
            "/api/deployments/plan/kubernetes", json={"recipe_id": RECIPE}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["applied"] is False
        assert "kind: Deployment" in body["yaml"]
        assert TOKEN not in resp.text

    def test_a_refusal_is_a_400(self):
        client = TestClient(create_app())
        resp = client.post(
            "/api/deployments/plan/kubernetes",
            json={"recipe_id": RECIPE, "hf_cache": "nfs"},
        )
        assert resp.status_code == 400
        assert "hf_cache" in resp.json()["detail"]

    def test_nothing_is_recorded(self):
        client = TestClient(create_app())
        client.post("/api/deployments/plan/kubernetes", json={"recipe_id": RECIPE})
        assert tools.deployment_records.load() == []
