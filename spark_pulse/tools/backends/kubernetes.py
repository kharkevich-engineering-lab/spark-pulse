"""A run rendered as Kubernetes manifests, and nothing applied (#164).

The only real test of the line #161 drew between a run's *intent* (what it is)
and a backend's *realisation* (how it runs) is a second backend that consumes
the intent without reaching past it. This one renders and stops there: no
cluster is called, nothing is applied, and the lifecycle methods refuse.

What it renders, from a :class:`~spark_pulse.tools.intent.RunIntent` alone:

* one rank: an ``apps/v1`` **Deployment** of one replica;
* several ranks of an engine that rendezvouses (vLLM, SGLang): a
  **LeaderWorkerSet** (``leaderworkerset.x-k8s.io/v1``) — the leader is rank
  zero, the workers are ranks one and up;
* a **Service** in front of the API port, selecting rank zero;
* the HF cache as a **PersistentVolumeClaim** or a **hostPath**, chosen by
  the request or by ``kubernetes.hf_cache`` in settings.

Every command is the engine's own rendering, through ``RunIntent.render``.
For several ranks the topology handed to it names pods rather than machines:
rank zero is ``$(LWS_LEADER_ADDRESS)``, which LeaderWorkerSet injects, and a
worker's own address is ``$(POD_IP)``, from the downward API — Kubernetes
expands both in ``env`` and ``args``. LeaderWorkerSet runs every worker from
one template, so the worker's arguments choose its rank's rendered command by
``$LWS_WORKER_INDEX`` rather than re-render one command with a rank patched
into it.

Whatever the manifests need that the intent does not carry is a **finding**,
reported with the plan and not worked around: those are defects of #161's
boundary, and finding them is what this backend is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from spark_pulse.tools.runtime_errors import NativeRuntimeError

if TYPE_CHECKING:
    from spark_pulse.tools.backends.runtime_checks import RuntimeReport
    from spark_pulse.tools.intent import RunIntent

__all__ = [
    "HF_CACHE_HOSTPATH",
    "HF_CACHE_PVC",
    "KubernetesBackend",
    "KubernetesPlan",
    "render_manifests",
]

#: The two ways the HF cache can reach a pod.
HF_CACHE_PVC = "pvc"
HF_CACHE_HOSTPATH = "hostPath"

#: Defaults for the cache and the token, overridable in settings.json under
#: ``kubernetes:`` (``hf_cache``, ``hf_cache_claim``, ``hf_cache_path``,
#: ``hf_token_secret``, ``namespace``).
DEFAULTS = {
    "namespace": "spark-pulse",
    "hf_cache": HF_CACHE_PVC,
    "hf_cache_claim": "spark-pulse-hf-cache",
    "hf_cache_path": "/var/lib/spark-pulse/huggingface",
    "hf_token_secret": "spark-pulse-hf-token",
}

#: The API group LeaderWorkerSet is served under.
LWS_API = "leaderworkerset.x-k8s.io/v1"

#: Where the cache is mounted in the container, the path the Docker backend
#: binds it to, so ``HF_HOME`` means the same thing on both.
CONTAINER_HOME = "/home/spark"
HF_CACHE_IN_CONTAINER = CONTAINER_HOME + "/.cache/huggingface"

#: Kubernetes object names: DNS-1123 labels, at most 63 characters.
_NAME_LIMIT = 63
_NOT_DNS = re.compile(r"[^a-z0-9-]+")


def _dns_name(*parts: str) -> str:
    name = _NOT_DNS.sub("-", "-".join(p for p in parts if p).lower()).strip("-")
    return name[:_NAME_LIMIT].rstrip("-") or "spark-pulse"


def _settings() -> dict[str, Any]:
    from spark_pulse.config import config

    block = config._data.get("kubernetes") or {}
    return {**DEFAULTS, **{k: v for k, v in block.items() if k in DEFAULTS}}


@dataclass
class KubernetesPlan:
    """The manifests for one run, and what the intent could not tell them."""

    deployment_id: str
    name: str
    recipe_id: str
    engine: str
    variant: str
    image: str
    namespace: str
    port: int
    intent: dict[str, Any]
    intent_hash: str
    manifests: list[dict[str, Any]]
    #: ``{"code", "detail"}``: a fact the manifests needed that the intent
    #: does not carry, or a part of the run this backend does not render.
    findings: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backend: str = "kubernetes"

    def yaml(self) -> str:
        import yaml

        class _Plain(yaml.SafeDumper):
            # The same label dict sits on an object and its pod template; a
            # manifest is read by people and kubectl, never with &anchors.
            def ignore_aliases(self, data: Any) -> bool:
                return True

        return yaml.dump_all(self.manifests, Dumper=_Plain, sort_keys=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "deployment_id": self.deployment_id,
            "name": self.name,
            "recipe_id": self.recipe_id,
            "engine": self.engine,
            "variant": self.variant,
            "image": self.image,
            "namespace": self.namespace,
            "port": self.port,
            "intent": self.intent,
            "intent_hash": self.intent_hash,
            "manifests": self.manifests,
            "yaml": self.yaml(),
            "findings": list(self.findings),
            "warnings": list(self.warnings),
            "applied": False,
        }


def _finding(findings: list[dict[str, str]], code: str, detail: str) -> None:
    findings.append({"code": code, "detail": detail})


def _api_port(intent: RunIntent) -> int:
    """The pin, else the recipe's preference, else the engine's default.

    No node is asked what it already serves: a pod has its own network
    namespace, so the port is the container's and never collides.
    """
    for port in (intent.port_pin, intent.port_preference):
        if port:
            return int(port)
    return int(intent.engine.spec.runtime.ports.api or 8000)


def _pod_topology(intent: RunIntent) -> Any:
    """The intent's topology with pods for machines (several ranks only)."""
    from spark_pulse.engines.base import NodeInfo, Topology

    leader = NodeInfo(host="leader", ip="$(LWS_LEADER_ADDRESS)")
    workers = [
        NodeInfo(host=f"worker-{rank}", ip="$(POD_IP)")
        for rank in range(1, intent.topology.size)
    ]
    return Topology(nodes=[leader, *workers])


def _env(rank_env: dict[str, str], *, token_secret: str) -> list[dict[str, Any]]:
    """The container's env: the downward API first, so ``$(POD_IP)`` expands."""
    env: list[dict[str, Any]] = [
        {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
        {"name": "HOME", "value": CONTAINER_HOME},
        {"name": "HF_HOME", "value": HF_CACHE_IN_CONTAINER},
    ]
    env += [{"name": k, "value": str(v)} for k, v in rank_env.items()]
    if token_secret:
        # Rank zero only: a worker loads nothing, the rule `models.worker_env`
        # keeps on Docker. A reference, never the value.
        env.append(
            {
                "name": "HF_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": token_secret,
                        "key": "token",
                        "optional": True,
                    }
                },
            }
        )
    return env


def _container(
    intent: RunIntent,
    *,
    port: int,
    args: str,
    env: list[dict[str, Any]],
    readiness: str | None,
) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "engine",
        "image": intent.image_ref,
        "command": ["/bin/bash", "-c"],
        "args": [args],
        "env": env,
        "ports": [{"name": "api", "containerPort": port}],
        "resources": {"limits": {"nvidia.com/gpu": 1}},
        "securityContext": {"capabilities": {"add": ["IPC_LOCK"]}},
        "volumeMounts": [
            {"name": "hf-cache", "mountPath": HF_CACHE_IN_CONTAINER},
            {"name": "shm", "mountPath": "/dev/shm"},
        ],
    }
    if readiness:
        probe = {"httpGet": {"path": readiness, "port": "api"}}
        # A model takes minutes to load: the startup probe holds the
        # readiness probe off for up to an hour, the deploy timeout's order.
        container["startupProbe"] = {
            **probe,
            "periodSeconds": 10,
            "failureThreshold": 360,
        }
        container["readinessProbe"] = {**probe, "periodSeconds": 10}
    return container


def _volumes(settings: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    if kind == HF_CACHE_HOSTPATH:
        cache = {
            "name": "hf-cache",
            "hostPath": {
                "path": settings["hf_cache_path"],
                "type": "DirectoryOrCreate",
            },
        }
    else:
        cache = {
            "name": "hf-cache",
            "persistentVolumeClaim": {"claimName": settings["hf_cache_claim"]},
        }
    return [cache, {"name": "shm", "emptyDir": {"medium": "Memory"}}]


def _pvc(settings: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": settings["hf_cache_claim"],
            "namespace": settings["namespace"],
            "labels": {
                "app.kubernetes.io/part-of": labels["app.kubernetes.io/part-of"]
            },
        },
        "spec": {
            # Every rank of every run reads it, from whichever node it lands on.
            "accessModes": ["ReadWriteMany"],
            "resources": {"requests": {"storage": "500Gi"}},
        },
    }


def _worker_args(commands: dict[int, str]) -> str:
    """One worker template, each index running its own rendered command."""
    cases = "\n".join(
        f"  {rank}) exec {command} ;;" for rank, command in sorted(commands.items())
    )
    return (
        'case "$LWS_WORKER_INDEX" in\n'
        f"{cases}\n"
        '  *) echo "no rank rendered for worker $LWS_WORKER_INDEX" >&2; exit 2 ;;\n'
        "esac"
    )


def render_manifests(
    intent: RunIntent, *, hf_cache: str | None = None
) -> KubernetesPlan:
    """The manifests for ``intent``. Pure apart from reading settings."""
    settings = _settings()
    kind = hf_cache or settings["hf_cache"]
    if kind not in (HF_CACHE_PVC, HF_CACHE_HOSTPATH):
        raise NativeRuntimeError(
            f"hf_cache must be {HF_CACHE_PVC!r} or {HF_CACHE_HOSTPATH!r}, not {kind!r}"
        )
    findings: list[dict[str, str]] = []
    warnings = list(intent.warnings)
    port = _api_port(intent)
    size = intent.topology.size
    name = _dns_name("spark-pulse", intent.deployment_id)
    namespace = settings["namespace"]
    labels = {
        "app.kubernetes.io/name": "spark-pulse-run",
        "app.kubernetes.io/instance": name,
        "app.kubernetes.io/part-of": "spark-pulse",
        "spark-pulse.deployment": intent.deployment_id,
    }
    annotations = {
        "spark-pulse.recipe": intent.recipe_id,
        "spark-pulse.intent-hash": intent.intent_hash(),
        "spark-pulse.engine": f"{intent.engine_name}/{intent.variant}",
    }
    readiness = intent.engine.spec.runtime.readiness or None

    # Where the model file is, on a node this backend cannot ask: the engine
    # is handed the model id (or keeps `-hf`) and resolves it in the cache.
    ranks = (
        intent.render(port, "")
        if size == 1
        else _render_on_pods(intent, port, findings)
    )
    _intent_findings(intent, findings)

    manifests: list[dict[str, Any]] = []
    if kind == HF_CACHE_PVC:
        manifests.append(_pvc(settings, labels))
    volumes = _volumes(settings, kind)
    token = settings["hf_token_secret"]

    if size == 1:
        rank = ranks[0]
        pod = {
            "metadata": {"labels": labels, "annotations": annotations},
            "spec": {
                "restartPolicy": "Always",
                "containers": [
                    _container(
                        intent,
                        port=port,
                        args=rank.command,
                        env=_env(rank.env, token_secret=token),
                        readiness=readiness,
                    )
                ],
                "volumes": volumes,
            },
        }
        manifests.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "replicas": 1,
                    # One GPU per node: a second pod must not start beside the
                    # first while it still holds the card.
                    "strategy": {"type": "Recreate"},
                    "selector": {"matchLabels": {"app.kubernetes.io/instance": name}},
                    "template": pod,
                },
            }
        )
        selector = {"app.kubernetes.io/instance": name}
    else:
        leader, workers = ranks[0], ranks[1:]
        worker_envs = {tuple(sorted(w.env.items())) for w in workers}
        if len(worker_envs) > 1:
            _finding(
                findings,
                "worker-env-differs",
                "the workers' rendered environments differ by rank, and a "
                "LeaderWorkerSet runs every worker from one template; rank "
                f"{workers[0].node_rank}'s is used for all of them",
            )
        manifests.append(
            {
                "apiVersion": LWS_API,
                "kind": "LeaderWorkerSet",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "labels": labels,
                    "annotations": annotations,
                },
                "spec": {
                    "replicas": 1,
                    "leaderWorkerTemplate": {
                        "size": size,
                        "restartPolicy": "RecreateGroupOnPodRestart",
                        "leaderTemplate": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "containers": [
                                    _container(
                                        intent,
                                        port=port,
                                        args=leader.command,
                                        env=_env(leader.env, token_secret=token),
                                        readiness=readiness,
                                    )
                                ],
                                "volumes": volumes,
                            },
                        },
                        "workerTemplate": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "containers": [
                                    _container(
                                        intent,
                                        port=port,
                                        args=_worker_args(
                                            {w.node_rank: w.command for w in workers}
                                        ),
                                        env=_env(workers[0].env, token_secret=""),
                                        readiness=None,
                                    )
                                ],
                                "volumes": volumes,
                            },
                        },
                    },
                },
            }
        )
        selector = {
            "leaderworkerset.sigs.k8s.io/name": name,
            "leaderworkerset.sigs.k8s.io/worker-index": "0",
        }

    manifests.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "selector": selector,
                "ports": [{"name": "api", "port": port, "targetPort": "api"}],
            },
        }
    )
    return KubernetesPlan(
        deployment_id=intent.deployment_id,
        name=intent.name,
        recipe_id=intent.recipe_id,
        engine=intent.engine_name,
        variant=intent.variant,
        image=intent.image_ref,
        namespace=namespace,
        port=port,
        intent=intent.to_dict(),
        intent_hash=intent.intent_hash(),
        manifests=manifests,
        findings=findings,
        warnings=warnings,
    )


def _render_on_pods(
    intent: RunIntent, port: int, findings: list[dict[str, str]]
) -> list[Any]:
    """Each rank's command, rendered for pods rather than machines."""
    from dataclasses import replace

    if intent.rpc_port:
        # llama.cpp's RPC backend: rank zero is handed every worker's address
        # up front, and a worker answers no HTTP. A LeaderWorkerSet's worker
        # pods have stable names, but the intent's topology says only how
        # many there are — not the per-worker DNS a head would dial.
        raise NativeRuntimeError(
            f"{intent.engine_name}/{intent.variant} spans nodes over RPC, which "
            "this backend does not render: the head needs each worker's address "
            "and the intent carries none a pod would have"
        )
    if not intent.rendezvous_port:
        raise NativeRuntimeError(
            f"{intent.engine_name}/{intent.variant} declares no rendezvous port, "
            "so there is no multi-node form to render"
        )
    on_pods = replace(intent, topology=_pod_topology(intent))
    _finding(
        findings,
        "topology-names-hosts",
        "the intent's topology names machines (address, interfaces, fabric), "
        "which is where a run goes rather than what it is; it was replaced with "
        "pods — rank zero at $(LWS_LEADER_ADDRESS), each worker at $(POD_IP) — "
        "to render the commands",
    )
    ranks = on_pods.render(port, "")
    if any("VLLM_HOST_IP" in r.env or "SGLANG_HOST_IP" in r.env for r in ranks):
        _finding(
            findings,
            "host-ip-is-a-name",
            "rank zero's own host IP renders as $(LWS_LEADER_ADDRESS), a DNS "
            "name; an engine that wants an address there needs the leader's pod "
            "IP, which only the leader knows",
        )
    return ranks


def _intent_findings(intent: RunIntent, findings: list[dict[str, str]]) -> None:
    """What the manifests needed and the intent does not say."""
    _finding(
        findings,
        "gpus-per-rank",
        "the intent does not say how many GPUs each rank uses; each pod "
        "requests one (nvidia.com/gpu: 1), which is a DGX Spark's count",
    )
    _finding(
        findings,
        "model-file-unknown",
        "where the model file sits is a question for the node that loads it "
        "(Docker asks its agent); no pod can be asked before it exists, so the "
        "engine is handed the model id and resolves it in the mounted cache",
    )
    ulimits = dict(intent.engine.container_profile().get("ulimits") or {})
    if ulimits:
        _finding(
            findings,
            "ulimits-not-settable",
            f"the engine's container profile asks for ulimits "
            f"({', '.join(f'{k}={v}' for k, v in sorted(ulimits.items()))}); a pod "
            "spec cannot set them, so they are the node runtime's defaults",
        )
    _finding(
        findings,
        "engine-user",
        "the Docker backend runs the engine as the node's agent user, resolved "
        "on the node; the intent names no uid, so the image's own user runs",
    )
    if intent.mods:
        _finding(
            findings,
            "mods-not-rendered",
            f"mods ({', '.join(intent.mods)}) are applied on Docker by exec into "
            "the running container; they are not rendered here (an init "
            "container and a ConfigMap would be needed)",
        )


class KubernetesBackend:
    """Renders a run as manifests. Applies nothing, runs nothing."""

    name = "kubernetes"

    def plan(
        self,
        recipe_id: str,
        *,
        engine: str | None = None,
        variant: str | None = None,
        model: str | None = None,
        params: dict[str, Any] | None = None,
        extra_args: list[str] | None = None,
        nodes: list[str] | None = None,
        solo: bool = True,
        allow_missing_model: bool = True,
        hf_cache: str | None = None,
    ) -> KubernetesPlan:
        from spark_pulse.engines import get_registry
        from spark_pulse.tools.intent import resolve_intent

        intent = resolve_intent(
            recipe_id,
            registry=get_registry(),
            engine=engine,
            variant=variant,
            model=model,
            params=params or {},
            extra_args=extra_args or [],
            nodes=nodes,
            solo=solo,
            allow_missing_model=allow_missing_model,
        )
        return render_manifests(intent, hf_cache=hf_cache)

    def realise(
        self, intent: RunIntent, services: Callable[[str], Any] | None = None
    ) -> KubernetesPlan:
        del services  # nothing on a node is asked
        return render_manifests(intent)

    def model_location(
        self,
        intent: RunIntent,
        node: str,
        services: Callable[[str], Any],
        warnings: list[str],
    ) -> tuple[str, str]:
        del intent, node, services, warnings
        return ("", "")

    def _refuse(self, what: str) -> Any:
        raise NativeRuntimeError(
            f"the kubernetes backend renders manifests only; it does not {what}"
        )

    def start(self, recipe_id: str, name: str, params: dict[str, Any], **_: Any):
        return self._refuse("start a run")

    def status(self, run_id: str):
        return self._refuse("read a run's status")

    def stop(self, run_id: str):
        return self._refuse("stop a run")

    def delete(self, run_id: str):
        return self._refuse("delete a run")

    def logs(self, run_id: str, lines: int = 200):
        return self._refuse("read logs")

    def list_runs(self) -> list[dict[str, Any]]:
        return []

    def diagnose(
        self,
        node_id: str,
        label: str = "",
        run: Callable[[str], Any] | None = None,
        facts: Any = None,
    ) -> RuntimeReport:
        """Nothing to check: a dry run needs no cluster (#164)."""
        from spark_pulse.tools.backends.runtime_checks import RuntimeReport

        return RuntimeReport("kubernetes", [], "")
