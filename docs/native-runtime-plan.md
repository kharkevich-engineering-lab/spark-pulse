# Spark Pulse as a self-contained model-serving tool

Status: plan v2, 2026-09-03. Research basis: the spark-pulse code on branch
`feat/docker-mgmt`, the `spark-pulse-recipes` repo, and upstream
`eugr/spark-vllm-docker` at `68e1b4c` (2026-09-03).

Decisions taken (2026-09-03):

1. Spark Pulse never builds images. Engine images come from a separate `spark-pulse-engine` repo (section 2.3) that builds them from source and publishes them. No `eugr/spark-vllm` image anywhere in the chain, not even as a base or fallback. Upstream's Dockerfile and patch queue are reference material only.
2. Recipe v1 stays valid forever via an importer; v2 adds structure. Existing OCI collections keep working.
3. No Ray. Multi-node is no-Ray only. Ray stays documented in appendix A for a future implementation.
4. Mesh (3 and 4 node) is in scope for native cluster.
5. Drop `git_update`. Upstream is an import source, not a runtime dependency.
6. Engines are selectable (vLLM, SGLang, more later). A recipe may name an engine but does not have to.
7. Model management is independent of recipes: download from the HF hub, a local HF mirror, or use a local path. A deployment is recipe + engine + model + params + nodes.

## 1. Where we are

### 1.1 What actually depends on the upstream checkout

Spark Pulse touches `spark_vllm_path` in four ways. Only the first is a real
runtime dependency; the rest are data-directory conventions.

| Kind | Where | What |
|---|---|---|
| Exec upstream bash | `tools/deployments.py:133-256` | The entire deploy path is `run-recipe.sh recipes/<id>.yaml --port ... --solo\|--nodes`. Health is PID liveness, stop is SIGTERM to the process group, logs are `tail` on our own log file. |
| Exec upstream bash | `tools/cache.py:104-119` | `hf-download.sh --cleanup`, effectively dead (case mismatch on the target name). |
| `git` in checkout | `tools/git_update.py` | fetch/pull/status, background timer, SSE, a UI page. To be removed. |
| Read-only scan | `tools/recipes.py`, `tools/mods.py`, `tools/launch_script.py` | `recipes/**`, `mods/<id>/`, `examples/`. |
| Symlinks into it | `tools/custom_files.py`, `tools/oci_registry.py:1016-1069` | `recipes/custom-*`, `recipes/oci-*`, `mods/custom-*` so upstream's runner can see our recipes. |

`launch-cluster.sh`, `build-and-copy.sh` and upstream mod runners are never
executed by Spark Pulse.

### 1.2 The native stack already exists but is not wired in

A second, Python-native stack was built on this branch and is not connected
to the deployment path the UI uses:

- `tools/docker.py` (Docker SDK, labels `spark-pulse.*`), `tools/remote_docker.py` (docker CLI over SSH)
- `tools/cluster.py` (`ClusterOrchestrator` with rollback), `tools/ray.py`, `tools/ssh.py`
- `tools/launch_script.py` (patching replaces `launch-cluster.sh` sed), `tools/mods.py` (`ModOrchestrator`)
- `tools/discovery.py` (network/IB), `tools/parallelism.py`, `tools/reconciliation.py`, `tools/health.py`, `tools/locking.py`, `tools/events.py`

Known defects that block using it for real:

1. `remote_docker.py` calls `DockerService` with the wrong signatures at lines 70, 163, 194, 253 and builds `ContainerInfo` with fields that do not exist (277-283). `cluster.py:343-356` reads the same non-existent fields.
2. Label namespace split: `docker.py`/`cluster.py` write `spark-pulse.*`, `reconciliation.py:18-27` reads `spark_pulse.*`. Reconciliation never matches anything.
3. `cluster.py:463-476` `_apply_mods` only logs.
4. `health.py:310` calls a module-level `get_cluster_status` that does not exist.
5. Frontend calls `POST /api/cluster/reconcile` and `GET /api/cluster/lock/{resource}`, neither route exists.
6. `routers/config.py:28` hardcodes `simulation_mode: true`.
7. Deployments pass `recipes/<id>.yaml` to upstream, which breaks for extensionless `custom-*`/`oci-*` symlinks.

### 1.3 Three recipe schemas that disagree

| Source | `recipe_version` | Notes |
|---|---|---|
| upstream `run-recipe.py` | required, `"1"` | everything lives in `command` as a `str.format` template; `{{ }}` for literal braces; solo forces `tensor_parallel=1`; strips `--distributed-executor-backend` unless `--ray` |
| `spark-pulse-recipes/spark-recipe.schema.json` | absent | required `name, container, command`; `additionalProperties: true` |
| `spark_pulse/tools/recipes.py` | optional, default `"1"` | lenient; own `{-tp}` and `{--gpu-memory-utilization}` placeholders that upstream does not have |

`spark-pulse-recipes` also has drift: README documents a Python packager that
does not exist, `index.yaml` bakes `ghcr.io/sparkrecipes/<recipe>` refs while
CI publishes one collection artifact to
`ghcr.io/kharkevich-engineering-lab/spark-pulse-recipes:<ver>`, and the Qwen
skill doc describes a per-recipe push flow that no longer matches the code.

### 1.4 Upstream semantics we reproduce natively

From `launch-cluster.sh` and `run-recipe.py`. This is the behavioural contract
for the vLLM engine on DGX Spark.

**Container spec (per node)**

```
docker run --privileged --ulimit nofile=1048576:1048576 --ipc=host   # or non-privileged: --cap-add=IPC_LOCK --shm-size=64g --device=/dev/infiniband --memory 110g --memory-swap 120g --pids-limit 4096
  --gpus all -d --rm --network host --name vllm_node --entrypoint=
  -e NCCL_IGNORE_CPU_AFFINITY=1 -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
  -e VLLM_HOST_IP=<ip>
  -e MN_IF_NAME=$ETH_IF -e UCX_NET_DEVICES=$ETH_IF -e NCCL_SOCKET_IFNAME=$ETH_IF
  -e NCCL_IB_HCA=$IB_IF -e NCCL_IB_DISABLE=0
  -e OMPI_MCA_btl_tcp_if_include=$ETH_IF -e GLOO_SOCKET_IFNAME=$ETH_IF -e TP_SOCKET_IFNAME=$ETH_IF
  -v $HF_HOME:/root/.cache/huggingface
  -v ~/.cache/vllm:/root/.cache/vllm -v ~/.cache/flashinfer:/root/.cache/flashinfer -v ~/.triton:/root/.triton -v ~/.tilelang:/root/.tilelang
  <image> sleep infinity        # or: earlyoom -M 524288,102400 -s 100 -r 60
```

(Ray-specific env vars are omitted; see appendix A.)

**Lifecycle**: idle container first, then `docker exec` for each mod
(`/workspace/mods/<name>/run.sh` with `WORKSPACE_DIR=<image workdir>`), then
`docker exec` of the rendered serve script copied to `/workspace/exec-script.sh`.
Output is redirected to `/proc/1/fd/1` so `docker logs` works.

**Topologies**

- solo: strip `--distributed-executor-backend`, force `tensor_parallel=1` unless overridden.
- multi-node (no-Ray): required nodes = tp*pp*dp (1 GPU/node), trim peers. Workers first with `--nnodes N --node-rank R --master-addr HEAD --master-port 29501 --headless`, head last with rank 0.
- mesh (3 or 4 nodes without a switch): `IB_IF` covers all four CX7 ports, `ETH_IF` is the 10G port, plus `NCCL_NET_PLUGIN=none`, `NCCL_IB_SUBNET_AWARE_ROUTING=1`, `NCCL_IB_MERGE_NICS=0`. Models cannot use tp=3, so 3-node means pp=3 or dp=3.
- pre-flight: SSH reachability, image ID parity across nodes (`docker image inspect --format '{{.Id}}'`), refuse to reuse a running container when PR mods are requested.

**Mods**: dir or zip with `run.sh`; runtime PR mods synthesised from
`vllm-project/vllm/pull/N.diff` with path allow-list (`vllm/**`, no native
sources) and sha256 check. Mods are vLLM-specific and belong to the vLLM engine.

**Images**: `eugr/spark-vllm:latest` tagged `vllm-node`; `eugr/spark-vllm-b12x:latest`
tagged `vllm-node-b12x`; `vllm-node-mxfp4` is build-only. Python 3.12, vLLM under
`/usr/local/lib/python3.12/dist-packages/vllm`, workdir `/workspace/vllm`.

**Models**: `uvx hf download` plus `rsync` to copy hosts; existence check
ignores `HF_HOME` (upstream bug).

**Autodiscovery**: needs `ibdev2netdev`, `nc`, passwordless SSH, exactly 2 or 4
Up CX7 interfaces, and is interactive (`read -p`). Not usable from a service.

### 1.5 SGLang on DGX Spark: hardware evidence

Source: `mark-ramsey-ri/sglang-dgx-spark` at `b58fe77` (2026-05-01). It is a bash
orchestration layer around the upstream `lmsysorg/sglang` image; it builds
nothing and applies no patches, so the image itself is usable as-is.

**Verified**: `lmsysorg/sglang:v0.5.10.post1-cu130` (SGLang 0.5.10, CUDA 13.0.1,
multi-arch with arm64) on 1 Spark tp=1 and 2 Sparks tp=2 with Llama-3.1-8B;
gpt-oss-120b on 2 Sparks with the older `lmsysorg/sglang:spark` tag. 4-node is
untested. The PyTorch "capability 12.1 outside (8.0)-(12.0)" warning is benign.

**Container spec** (differs from the vLLM one in 1.4):

```
docker run -d --restart no --name sglang-head|sglang-worker-<host>
  --gpus all --network host --ipc=host --shm-size=32g
  --ulimit memlock=-1 --ulimit stack=67108864
  --device=/dev/infiniband                      # not privileged
  -v $HF_HOME:/root/.cache/huggingface -v ~/tiktoken_encodings:/tiktoken_encodings
  -e HF_HOME=/root/.cache/huggingface -e TIKTOKEN_ENCODINGS_BASE=/tiktoken_encodings
  -e NCCL_IB_DISABLE=0 -e NCCL_NET_GDR_LEVEL=5 -e NCCL_TIMEOUT=1200000
  -e NCCL_SOCKET_IFNAME=$ETH_IF -e GLOO_SOCKET_IFNAME=$ETH_IF -e NCCL_IB_HCA=$IB_IF
  lmsysorg/sglang:v0.5.10.post1-cu130
  python3 -m sglang.launch_server --model-path M --tp T --pp-size 1
    --nnodes N --node-rank R --dist-init-addr HEAD:50000
    --host 0.0.0.0 --port 30000 --mem-fraction-static 0.90
    [--reasoning-parser gpt-oss --tool-call-parser gpt-oss] [--disable-cuda-graph] [--enable-dp-attention]
```

**Contract points for the engine plugin**

- Command runs as the container command, not idle-then-exec. Our runtime can still use idle-then-exec; the image has no mods so nothing is lost either way.
- `--nnodes/--node-rank/--dist-init-addr` are passed even for a single node. Rendezvous port 50000, API port 30000, both on host network.
- Multi-node: workers rank 1..N start first, head rank 0 last, identical args except rank. No Ray. Same order our cluster code already uses.
- Multi-node workaround: FlashInfer all-reduce fusion uses CUDA IPC and fails across nodes (`invalid device context`). Add `--enable-dp-attention`, and `--disable-cuda-graph` when needed. Solo does not need either.
- NCCL hang fallback: `NCCL_IB_DISABLE=1` (10 to 20x slower); the log line `NET/IB` versus `NET/Socket` tells which path is active. Worth surfacing in health.
- Readiness is `GET /health`, model id from `GET /v1/models`. `/metrics` unused there but SGLang exposes it with `--enable-metrics`.
- gpt-oss needs tiktoken files mounted offline; some models needed `--mem-fraction-static 0.85`.
- Page-cache pressure on UMA shows up as OOM; `drop_caches` before loading large models. The vLLM stack has the same trick as a mod; it belongs in the runtime pre-flight for both engines.

## 2. Target shape

```
Models   (catalogue + download jobs; sources: HF hub, HF mirror, local path)
Recipes  (schema owned by spark-pulse; v1 imported, v2 structured; engine optional)
Engines  (plugins: vllm, sglang, ...; each knows image, launch, readiness, multi-node)
Nodes    (registry + non-interactive discovery + NCCL env)
   |
   v
Deployment request = recipe + engine (override allowed) + model (override allowed) + params + nodes
   -> Plan     (resolve engine, image, model snapshot, mods, topology, ports; dry-run returns it)
   -> Preflight(ssh, image parity, model present on every node, gpu mem, port free)
   -> Runtime  (Docker SDK local + docker-over-SSH remote: idle container, mods, exec)
   -> Record   (container labels are the source of truth, deployments.json is a cache)
   -> Health   (docker state + engine readiness probe + engine metrics), logs (docker logs), events (SSE)
Surfaces: REST -> MCP (already REST-backed), CLI (`spark-pulse deploy`), UI
```

### 2.1 Engine abstraction

An engine is a Python class registered by name. It owns everything that is
specific to a serving framework; the runtime and cluster code are engine-agnostic.

```
class Engine:
    name: str                                   # "vllm", "sglang"
    default_image(topology) -> ImageRef         # e.g. eugr/spark-vllm:latest for vllm
    cache_mounts() -> list[Mount]               # vllm/flashinfer/triton/tilelang for vllm
    base_env(node: NodeInfo) -> dict            # VLLM_HOST_IP etc.
    render(recipe, model, params, topology, node_rank) -> LaunchScript
    readiness(port) -> Probe                    # GET /v1/models (both), /health
    metrics(port) -> Probe | None               # /metrics
    supports(recipe) -> bool                    # can this engine run this recipe?
    supports_mods: bool                         # only vllm today
```

Multi-node args differ per engine: vLLM uses `--nnodes/--node-rank/--master-addr/--master-port/--headless`, SGLang uses `--nnodes/--node-rank/--dist-init-addr`. Each engine's `render` produces the per-rank script; the cluster code only orders the starts (workers first, head last) and waits for readiness on the head.

Engine selection order: request override > recipe `engine` > config `default_engine` (vllm). If the engine cannot run the recipe, `supports()` refuses at plan time with a reason, before anything starts.

### 2.2 Recipe v2 and engine portability

A v1 recipe is a vLLM `command` template and is implicitly `engine: vllm`.
Running it on SGLang is not possible because the flags are vLLM's; the plan
step rejects it.

A v2 recipe separates what is engine-neutral from what is not:

```yaml
recipe_version: "2"
name: Qwen3.5-122B-FP8
model: Qwen/Qwen3.5-122B-A10B-FP8          # default model, overridable at deploy
engine: vllm                               # optional; default engine when omitted
constraints: {solo_only: false, cluster_only: true, min_nodes: 2}
params:                                    # engine-neutral, exposed in the UI form
  port: 8000
  host: 0.0.0.0
  tensor_parallel: 2
  max_model_len: 262144
  gpu_memory_utilization: 0.8
engines:
  vllm:
    image: eugr/spark-vllm:latest          # optional, engine default otherwise
    mods: [fix-qwen3.5-chat-template]
    env: {}
    args: >-                               # engine-specific tail, may use {params}
      --load-format instanttensor --enable-prefix-caching
      --tool-call-parser qwen3_coder --reasoning-parser qwen3
      --chat-template unsloth.jinja --max-num-batched-tokens 8192
  sglang:
    image: ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/sglang:0.5.10
    env: {}
    args: >-
      --tool-call-parser qwen25 --reasoning-parser qwen3 --mem-fraction-static 0.85
```

The engine renders `serve <model> <neutral params mapped to its flags> <args>`.
A recipe with only `engines.vllm` runs only on vLLM; the UI shows the engine
picker limited to the engines the recipe lists, plus "generic" when the engine
can serve the model from neutral params alone (useful for plain HF models with
no special flags).

`command:` remains valid in v2 as an escape hatch and pins the recipe to the engine it is written for.

### 2.3 `spark-pulse-engine` repo

A standalone repo, sibling of `spark-pulse-recipes`, that owns every Dockerfile
and publishes engine images plus a machine-readable engine index. Spark Pulse
consumes the index the same way it consumes recipe collections today.

```
spark-pulse-engine/
  engines/
    vllm/
      Dockerfile            # multi-stage from nvidia/cuda:13.x-devel-ubuntu24.04, builds NCCL, flashinfer, vllm from pinned refs
      patches/              # our own patch queue against the pinned vllm ref
      engine.yaml           # metadata, see below
    vllm-b12x/              # variant: vllm from local-inference-lab fork plus b12x kernels
    sglang/
      Dockerfile            # FROM lmsysorg/sglang:<pinned digest> (arm64/cu13), adds tiktoken files and Spark defaults
      engine.yaml
  spark-engine.schema.json
  index.yaml                # generated, like the recipes index
  packages/{validate,inventory,publish}
  .github/workflows/{validate,build,publish}.yml
```

`engine.yaml` per image:

```yaml
engine: vllm                      # engine plugin name in spark-pulse
variant: default                  # default | b12x | mxfp4
image: ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm
version: 0.3.0                    # semver of this image, tag
sources:                          # everything pinned; no third-party runtime image for vllm
  vllm: {repo: vllm-project/vllm, ref: <sha>}
  flashinfer: {repo: flashinfer-ai/flashinfer, ref: <sha>}
  nccl: {repo: NVIDIA/nccl, ref: <sha>}
  torch: {version: 2.13.0, index: https://download.pytorch.org/whl/cu130}
  base: nvidia/cuda:13.0.2-devel-ubuntu24.04@sha256:...
runtime:
  python: "3.12"
  workdir: /workspace/vllm
  site_packages: /usr/local/lib/python3.12/dist-packages
  cache_mounts: [~/.cache/vllm, ~/.cache/flashinfer, ~/.triton, ~/.tilelang]
  env: {PYTORCH_CUDA_ALLOC_CONF: "expandable_segments:True", NCCL_IGNORE_CPU_AFFINITY: "1"}
  serve: "vllm serve"
  readiness: /v1/models
  metrics: /metrics
  multi_node: {style: torchrun, master_port: 29501}
capabilities: {mods: true, pr_mods: true, solo: true, cluster: true}
arch: [linux/arm64]
gpu_arch: ["12.1a"]
```

**vLLM build ownership.** The image is built from source: CUDA devel base,
NCCL from source for `sm_121`, flashinfer and vLLM wheels at pinned refs with
`TORCH_CUDA_ARCH_LIST=12.1a`, then a slim runner stage. Upstream's Dockerfile
at `68e1b4c` is the reference for the stage layout and for which patches are
needed today (`docker/patch_vllm_*.py`, DeepGEMM pin, NCCL soname fix, cutlass
DSL pin). We re-derive our own `patches/` from it, track upstream vLLM PRs
until they merge, and drop patches as they land. Wheel caching between builds
(`--output type=local` export stages, restored in CI) keeps rebuilds to the
runner stage when only the patch set changes.

CI: build on arm64 runners (or a self-hosted Spark), smoke test (`vllm --version`,
a tiny model served and probed on `/v1/models`), push to ghcr.io with semver and
digest, publish `index.yaml` as an OCI artifact next to the images. A vLLM from-source
build on a GitHub arm64 runner is multi-hour; the wheel cache and a weekly
schedule plus manual dispatch keep it tolerable.

Spark Pulse side: the engine registry (2.1) is populated from bundled defaults
plus any configured engine indexes (reusing `tools/oci_registry.py`). The
`engine.yaml` metadata feeds `default_image`, `cache_mounts`, `base_env`,
`readiness` and `multi_node` so the Python engine class holds only rendering
logic. Image updates become "a new version in the index", with the same
background update check the recipe collections already have.

Boundary: Dockerfiles, patch queues and build args live only in
`spark-pulse-engine`. Spark Pulse pulls by digest and never runs `docker build`.

Compatibility with v1 recipes: `container: vllm-node` and `vllm-node-b12x` map
to our `vllm` and `vllm-b12x` images through the image catalogue's tag mapping
(3.5). Mods that hardcode `/usr/local/lib/python3.12/dist-packages` keep working
because `engine.yaml` `runtime.site_packages` is set to the same path in our image.
`vllm-node-mxfp4` becomes a third variant once its base
(`nvcr.io/nvidia/pytorch`) is pinned and built in the engines repo.

## 3. Functional areas to support

"Have" means it exists today in some form on this branch.

### 3.1 Runtime (native Docker)
- Container spec builder producing the spec in 1.4 from config (`docker:` and `nccl:` blocks in `config.yaml` already exist) plus engine-provided mounts and env. Privileged and non-privileged profiles, `--publish` in solo, `--keep-entrypoint`, `earlyoom`, extra docker args. Have: `docker.py` partial.
- Idle-then-exec lifecycle with output to `/proc/1/fd/1`. Not have.
- Log streaming from `docker logs` over SSE, replacing the file `tail`. Have: `events.py`, `sse.py`.
- Stop, restart, force-remove with rollback. Have: `cluster.py` rollback.
- Local via SDK, remote via SSH; one `ContainerService` interface with the signature bugs in 1.2 fixed and a contract test that runs local, remote and mock through the same scenarios.

### 3.2 Engines
- `engines/base.py` interface (2.1), `engines/vllm.py` with upstream-exact rendering (`str.format`, `{{ }}` escapes, extras `shlex.quote`d, solo rewrite, `--distributed-executor-backend` stripped), `engines/sglang.py`.
- Engine registry exposed at `GET /api/engines` with per-engine image defaults and capabilities, consumed by the deploy form.
- SGLang on GB10 is proven for solo and 2-node on `lmsysorg/sglang:v0.5.10.post1-cu130` (section 1.5). The `spark-pulse-engine` sglang image wraps that tag, adds the tiktoken files, and pins `--enable-dp-attention` for multi-node in `engine.yaml`. Ship behind `engines.sglang.enabled` only until our own smoke test passes on a Spark.
- Engine-specific container profile: SGLang runs non-privileged with `--device=/dev/infiniband`, `memlock=-1`, `stack=67108864`, `shm 32g`. The container spec builder therefore takes its resource profile from the engine (`engine.yaml` `runtime.container`) with config overrides, not from a single global `docker:` block.
- Engine-specific readiness: vLLM `/v1/models`, SGLang `/health`. Engine-specific ports: vLLM 8000, SGLang 30000 plus rendezvous 50000; the port allocator reserves both.
- Future candidates: llama.cpp server, TensorRT-LLM. Not planned.

### 3.3 Recipes
- One canonical schema, versioned, owned in `spark_pulse/schemas/` and consumed by `spark-pulse-recipes`. v1 = today's upstream format, v2 = section 2.2.
- v1 importer: reads upstream `recipes/**` and `mods/` from an optional import path or a git URL, converts to an internal model, never symlinks. Drop the spark-pulse-only `{-tp}` placeholders or map them here.
- Dry-run endpoint that returns the resolved plan: engine, image, model snapshot, rendered per-rank scripts, docker spec. Tests use it instead of upstream `--dry-run`.
- Sources: bundled, local user dir, OCI collections (have), upstream import.
- Customization overlay (have, `custom_recipes.py`) kept on top of the new schema.

### 3.4 Models
- Model catalogue independent of recipes: id, source, revision, local snapshot path, size, which recipes reference it, which nodes have it.
- Sources: HF hub, HF mirror (`HF_ENDPOINT` per source, token per source), local path (no download, mounted read-only). Configured in settings.
- Download jobs in-process via `huggingface_hub` with progress over SSE, cancel, resume, `HF_HOME` respected. Replaces `uvx hf` and `hf-download.sh`.
- Distribution to nodes (`rsync` over SSH, parallel) as a job; pre-flight verifies the snapshot on every node.
- Deploy-time override: pick any catalogued model compatible with the recipe (same architecture family is the user's responsibility; the UI warns when the id differs from the recipe default).
- Offline mode: `HF_HUB_OFFLINE=1` in the container when the source is local or the user asks.
- Disk-space pre-flight, cache inventory and cleanup (have: `cache.py` partial).

### 3.5 Images
- Catalogue populated from engine indexes (2.3) and manual entries: pull by tag or digest, tag mapping (`vllm-node` -> `ghcr.io/kharkevich-engineering-lab/spark-pulse-engine/vllm@sha256:...`) so v1 recipes keep resolving, inspect, show build metadata.
- Version tracking per engine image with update notifications, reusing the OCI collection update checker.
- Parity check and distribution to nodes (`docker save | ssh docker load`, skip when IDs match).
- No building in Spark Pulse and no third-party runtime images for vLLM. Recipes that need an image not yet published by `spark-pulse-engine` (for example `mxfp4`) are marked "image required".

### 3.6 Mods (vLLM engine)
- Native applier for dir and zip mods (have `ModOrchestrator`), wired into deploy (fix 1.2 item 3).
- Runtime PR mods with the same validation and checksum rules as upstream.
- Custom mods in `~/.config/spark-pulse/custom-mods` (have), zip upload (stub today), security scan (have).

### 3.7 Cluster
- Node registry in settings: name, IP, SSH user, interfaces, mesh flag. Replaces `.env` `CLUSTER_NODES`.
- Non-interactive discovery reusing `discovery.py` (subnet scan for GB10 nodes, CX7 detection, 2-port versus 4-port mesh) saving into the registry, never prompting.
- NCCL and interface env generation per node, including mesh settings.
- Topology planner: tp*pp*dp -> node count, peer trimming, port allocation from the configured range, engine-specific multi-node args.
- Pre-flight: SSH, image parity, model presence, GPU free memory, port free.
- Health: docker state on every node, engine readiness on head.

### 3.8 Deployments (unify the two stacks)
- One `Deployment` model: id, recipe ref, engine, model, rendered scripts, node set, container names, status, ports. Persist to `deployments.json` as a cache, rebuild from container labels at startup (fix the label namespace).
- Create -> plan -> pre-flight -> runtime -> readiness -> ready. Every step emits an event; the UI already has the operation state machine.
- Stop, restart, delete, logs, per-node status.
- Migration flag `runtime: upstream | native`, default `upstream` until native passes the E2E suite, then flip and delete the upstream path.

### 3.9 Observability and benchmarking
- Engine metrics probe into the SSE metrics stream.
- Readiness via the engine probe, liveness via docker state, optional restart policy.
- Benchmarking: keep the gated feature, add an `llama-benchy` runner against a running deployment.

### 3.10 Interfaces
- CLI: `spark-pulse deploy <recipe> [--engine] [--model] [--nodes] [--solo] [--port] [--dry-run]`, `stop`, `status`, `logs`, `models pull|list|sync`, `images pull|list|sync`, `nodes discover|list`.
- MCP gains the same tools automatically since it calls REST.
- UI: deploy form with engine and model pickers driven by the plan endpoint; Jobs page shows per-node containers; new Models and Images pages; Settings gets node registry and model sources; Git Update page removed, replaced by "Import from upstream" under Recipes.

## 4. Phases

Each phase is shippable and keeps `runtime: upstream` working until phase 4.

**Phase 0, stabilise the native stack (this branch).** Fix every item in 1.2. Contract test across `DockerService`, `RemoteDockerService` and the mock. Wire the missing cluster routes. Remove `git_update` and its UI page.

**Phase 1, engine interface and native solo deploy.** `engines/base.py`, `engines/vllm.py` with upstream-exact rendering, dry-run endpoint, container spec builder, idle-then-exec with mods, logs from `docker logs`, readiness probe. `runtime: native` flag. E2E in simulation, manual run on a Spark.

**Phase 2, models and images.** Model catalogue, sources (hub, mirror, local), download jobs, cache inventory. Image catalogue, pull by digest, parity, distribution over SSH. Pre-flight wired into the deploy pipeline. Models and Images pages.

**Phase 2b, `spark-pulse-engine` repo.** Bootstrap the repo: from-source vLLM Dockerfile with our own patch queue, `engine.yaml`, schema, index, build and publish workflows modelled on `spark-pulse-recipes`, wheel cache. First milestone is parity with upstream's current image on the recipes we care about, checked on a Spark. Spark Pulse reads the index into the engine registry. Starts alongside phase 1 because phase 1's hardware test needs an image; until the first publish, phase 1 is tested against a locally built image from this repo.

**Phase 3, recipe v2 and second engine.** Schema in `spark_pulse/schemas/`, v1 importer, v2 renderer, `engines/sglang.py` behind a flag, engine and model pickers in the deploy form. `spark-pulse-recipes` validates against the published schema, gains `recipe_version`, index/ref mismatch and stale docs fixed.

**Phase 4, native cluster.** Node registry, non-interactive discovery including mesh, no-Ray topology with engine-specific rank args, per-node env. Flip the default to `native`, delete `run-recipe.sh` invocation and the symlink code.

**Phase 5, extras.** Metrics scraping, benchmark runner, OCI-distributed mods, `vllm-mxfp4` and further engine variants.

## 5. Open questions

1. SGLang on GB10: resolved by section 1.5 for solo and 2-node. Still open: 3 and 4 node mesh, and whether `--enable-dp-attention` costs throughput enough to expose it as a recipe toggle rather than a default.
2. Model override safety: warn only, or require the override to match the recipe's architecture (read `config.json` from the snapshot)?
3. Where the recipe v2 and engine schemas live for the two sibling repos to consume: JSON schema files published from spark-pulse releases, or a shared package.
4. `spark-pulse-engine` CI hardware: GitHub arm64 runners can build but the vLLM build is multi-hour and the smoke test needs a GB10. Options: a self-hosted runner on a Spark for both, or build in CI and smoke test on a Spark before the index is promoted.
5. Patch queue upkeep: upstream carries a dozen vLLM source patches and a DeepGEMM pin today. Someone has to re-validate them on every vLLM ref bump. Decide the cadence (track vLLM releases, not main) and whether to automate a "rebase patches and build" PR.

## 5b. Findings from the first native deploy on hardware (2026-09-03)

Verified on a real GB10 against `feat/docker-mgmt`. Four defects were found
that no simulation test caught, all fixed:

1. Index entries without a digest produced `image:image:tag` refs, because the published index carries `tag` as a full reference while the spec wants a bare tag.
2. The engine's declared HF cache and `HF_HOME` both bound `/root/.cache/huggingface`; docker refuses two binds on one destination.
3. `DockerService.run_container` called `create_host_config`, which only exists on the low-level API client. Container start failed immediately.
4. Extra label constraints were passed as top-level docker filter keys, which the API rejects with a 400. Startup reconciliation silently matched nothing.

Still open, worth fixing before the native path becomes the default:

- **Image pulls block the deploy silently.** `containers.run` pulls implicitly, so a deploy against an image the host has not got sits with no output for the tens of minutes a 26 GB engine image takes. `plan()` should report whether the image is present locally, and the runtime should pull explicitly with progress events before starting the container.
- **Digest drift.** Republishing an engine version changes its digest, so a host that pulled `0.1.0` yesterday needs a fresh pull today for the same version string. Either treat a version as immutable and bump it on every publish, or surface "a newer digest is available" in the images UI.

## 6. Things to fix regardless of the plan

- `cache.py` wheels target name mismatch.
- `routers/config.py` hardcoded simulation flag.
- `AGENTS.md` architecture tree and pages list are stale.
- `spark-pulse-recipes` README and Qwen skill describe tooling that does not exist.

## Appendix A. Ray topology (not implemented, kept for reference)

Upstream supports a Ray executor for multi-node vLLM behind `--ray`. To add it later:

- per-node env: `RAY_NODE_IP_ADDRESS=<ip>`, `RAY_OVERRIDE_NODE_IP_ADDRESS=<ip>`, `RAY_memory_monitor_refresh_ms=0`, `RAY_num_prestart_python_workers=0`, `RAY_object_store_memory=1073741824`
- head: `docker exec -d C bash -c "ray start --block --head --port 29501 --object-store-memory 1073741824 --num-cpus 2 --node-ip-address HEAD --include-dashboard=false --disable-usage-stats >> /proc/1/fd/1 2>&1"`
- workers: `ray start --block --object-store-memory 1073741824 --num-cpus 2 --disable-usage-stats --address=HEAD:29501 --node-ip-address <ip>`
- poll `docker exec C ray status` up to 30 times at 2s, then 5s more
- append `--distributed-executor-backend ray` to the serve command when missing; run the serve command on the head only
- `tools/ray.py` on this branch already implements the start and status calls and can be reused
