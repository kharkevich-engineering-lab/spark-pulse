# Cluster parity with `eugr/spark-vllm-docker`

Status: 2026-09-04. Reference read at commit `358bf26`.

Spark Pulse drives Docker from Python; `spark-vllm-docker` is a bash wrapper.
This document exists so a reviewer can check us against it **without reading
1,294 lines of bash**. Every row names the reference by file and line, says
what we do, and carries a status.

## How to read the status column

Three states, and they carry very different risk. Do not collapse them.

| | Meaning |
|---|---|
| **S — specified** | A source establishes the required behaviour: upstream by file and line, or NVIDIA/NCCL/vLLM/SGLang documentation or source. We implemented what the source says. Hardware would confirm that it *works*, not discover what it should *be*. |
| **U — unspecified** | Nobody documents it. The row says which sources were checked and what question they failed to answer. These are the genuinely unknown ones. There are six. |
| **D — divergence** | Upstream does it for a reason that does not apply to us, or two sources disagree and we chose. The row says why. |

**Nothing in this document is hardware evidence.** One DGX Spark exists. Every
behaviour below is rendered, ordered, refused and recorded in simulation and
none of it has run on two machines.

## A second evidence pass

A parallel review produced a dossier of places where an authoritative source
contradicts our implementation, intended for `docs/cluster-evidence.md` on the
`docs/cluster-evidence` branch. **That file was not present on the branch when
this document was written** — the branch held only the two phase E commits — so
what is folded in below came from the review's enumerated findings rather than
from reading it. Where the dossier lands, it is the citation for §6 and §7 and
this document should point at it rather than restate it.

Ten findings were raised. Eight are fixed here (§7.1–7.6 and §6.1–6.3, plus the
selector grammar in §1a and the four-node decision in §4.8). Two are recorded
and deliberately not acted on: §5.6 and §2.12.

## Sources consulted

| Tier | Source |
|---|---|
| Reference | `eugr/spark-vllm-docker` @ `358bf26`: `launch-cluster.sh` (1294 lines), `autodiscover.sh` (451), `docs/NETWORKING.md` (446), `docs/AGENT_RUNBOOK.md` (373) |
| NVIDIA official | [DGX Spark known issues](https://docs.nvidia.com/dgx/dgx-spark/known-issues.html) · [Sync cluster assistant](https://docs.nvidia.com/sync/latest/cluster-assistant.html) · [NCCL for three Sparks](https://build.nvidia.com/spark/nccl/three-sparks) · [NCCL env vars 2.31.2](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html) |
| NVIDIA playbooks | [`dgx-spark-playbooks`](https://github.com/NVIDIA/dgx-spark-playbooks): `nvidia/connect-two-sparks`, `nvidia/connect-three-sparks`, `nvidia/multi-sparks-through-switch`, `nvidia/nccl/assets/launch.sh`, `connect-two-sparks/assets/performance_benchmarking_guide.md` |
| Upstream libraries | [NCCL source](https://github.com/NVIDIA/nccl) (`net_ib/init.cc`, `net_ib/connect.cc`, `plugin/net.cc`, `misc/socket.cc`) · [vLLM](https://github.com/vllm-project/vllm) (`engine/arg_utils.py`, `config/parallel.py`, `distributed/parallel_state.py`, `entrypoints/cli/serve.py`) · [SGLang](https://github.com/sgl-project/sglang) @ `v0.5.10.post1` · [PyTorch `torch.distributed`](https://docs.pytorch.org/docs/stable/distributed.html) |
| Community | [6–8 node Spark forum thread](https://forums.developer.nvidia.com/t/6x-spark-setup/354399) · [`mark-ramsey-ri/sglang-dgx-spark`](https://github.com/mark-ramsey-ri/sglang-dgx-spark) · [`alexellis/glm-5.3-flash-4x-dgx-spark-switchless`](https://github.com/alexellis/glm-5.3-flash-4x-dgx-spark-switchless) |

---

## 1. Interface discovery

| # | Behaviour | Reference | Spark Pulse | Status |
|---|---|---|---|---|
| 1.1 | Read `ibdev2netdev`, keep the ports it reports **Up**, in printed order | `autodiscover.sh:72` | `discovery.parse_ibdev2netdev`, `FabricConfig.up_ports` — same order, same filter | **S** |
| 1.2 | **Both RoCE twins of a cabled port go into `NCCL_IB_HCA`**, comma-joined | `autodiscover.sh:128`; `NETWORKING.md:38` gives the literal `NCCL_IB_HCA=rocep1s0f1,roceP2p1s0f1` | `FabricConfig.ib_hca` is every up RoCE device; `NodeRecord.infiniband_interfaces` holds them; the plan joins them | **S** — and see §7.1, this was broken |
| 1.3 | Four ports up ⇒ mesh; two ⇒ single cable; anything else refused by number | `autodiscover.sh:121,160,192` | `build_fabric_config` — same three branches, same refusal naming the count | **S** |
| 1.4 | Mesh names all four RoCE devices | `autodiscover.sh:167` (hardcoded `rocep1s0f0,roceP2p1s0f0,rocep1s0f1,roceP2p1s0f1`) | Every up device, discovered rather than hardcoded. Identical on the documented hardware; correct elsewhere. Order differs (`ibdev2netdev` order vs upstream's literal) and `NCCL_IB_HCA` is a filter, so order does not select differently | **S** |
| 1.5 | Single cable: management link is the addressed twin **without a capital P**, else the first addressed one | `autodiscover.sh:132-155`; `NETWORKING.md:37` | `build_fabric_config`, `_has_capital_p` | **S** |
| 1.6 | Mesh: management link is `enP7s7`, else `wlP9s9` with a warning, else refuse | `autodiscover.sh:171-184`; `NETWORKING.md:434` "*we have to use 10G interface for OOB*" — and NVIDIA's own ring playbook exports `NCCL_SOCKET_IFNAME=enP7s7` | `MESH_MANAGEMENT_INTERFACES`, same order, same warning | **S** |
| 1.7 | Every up `enp*` (no capital P) must carry an IP, else refuse | `autodiscover.sh:94-101` | `build_fabric_config` error; pre-flight `CHECK_FABRIC` FAIL | **S** |
| 1.8 | No two CX7 links may share a subnet, else refuse | `autodiscover.sh:103-117`; `NETWORKING.md:133` in bold | `build_fabric_config` error; pre-flight FAIL | **S** |
| 1.9 | Peer discovery by SSH-sweeping the subnet for `nvidia-smi` reporting `NVIDIA GB10` | `autodiscover.sh:223-254` | mDNS `_spark-pulse._tcp` and `_ssh._tcp` browse plus manual entry (`discovery.browse_peers`). No subnet sweep | **D** — a sweep SSHes into every host on the LAN. `cluster-agent-plan.md` §3.1 chose mDNS, which DGX OS already advertises, and typing an address always works |
| 1.10 | Interactive per-node confirmation, saved to `.env` | `autodiscover.sh:364-442` (`read -p`) | Node registry (`nodes.json`), REST `POST /api/nodes`. Never prompts | **D** — a service cannot answer `read -p`; `native-runtime-plan.md` §1.4 records this |
| 1.11 | Read the *head's* interfaces and hand the same `ETH_IF`/`IB_IF` to every node | `launch-cluster.sh:957-974` — one `get_env_flags` over globals | Each rank is pinned from **its own** registry record | **D** — correct only while every Spark is cabled identically. Ours is per node, and the pre-flight asks each node rather than assuming |

### 1a. Where we ask a node what upstream assumes

Upstream never asks a *peer* about its own interfaces (1.11). We do, in the
pre-flight: `discovery.FABRIC_COMMAND` runs locally or over SSH and produces a
`FabricConfig` per node. That gives four checks upstream cannot have, all in
`preflight._check_fabric`, and the status mapping is deliberate:

> **Where `autodiscover.sh` `return 1`s, we FAIL. Where upstream silently does
> the right thing and we might not have, we WARN and name the value to set.**

| Check | Status | Why |
|---|---|---|
| Unaddressed `enp*` twin | FAIL | `autodiscover.sh:97` returns 1 |
| Two links on one subnet | FAIL | `autodiscover.sh:113` returns 1 |
| Port count neither 2 nor 4 | FAIL | `autodiscover.sh:193` returns 1 |
| Mesh with no `enP7s7`/`wlP9s9` | FAIL | `autodiscover.sh:181` returns 1 |
| One twin pinned, not both | WARN | Upstream always passes both, so it never has to refuse. Halved bandwidth, not a failure |
| Mesh cabling without the mesh NCCL settings | WARN | Same |
| Wireless coordination | WARN | `autodiscover.sh:179` warns |
| No ConnectX at all | WARN | Upstream refuses (it has nowhere to get `ETH_IF`); we take names from the registry, so the honest answer is "this runs over sockets and is far slower" |

The names themselves are now parsed as the selector grammar NCCL documents
rather than as a comma-separated list of device names, because they are not
one: a leading `^` inverts the list into an exclusion (so "does it exist" is
the wrong question and the check is skipped), a leading `=` forces exact
matching and is not part of a name, an entry may carry `:port[:rail[:plane]]`,
and matching is otherwise by **prefix** — the documentation's own warning is
that `mlx5_1` also matches `mlx5_10`. `preflight.selector_names` and
`_resolves` implement that. Before this, a perfectly valid `=rocep1s0f1:1`
failed the check.

---

## 2. The NCCL environment

Upstream's per-node flags are `launch-cluster.sh:957-974`. We render the same
set, per rank, from `engines/vllm.py:_fabric_env` and `engines/base.py`.

| # | Variable | Reference | Spark Pulse | Status |
|---|---|---|---|---|
| 2.1 | `VLLM_HOST_IP=<node ip>` | `:960` | Same, per rank | **S** |
| 2.2 | `NCCL_SOCKET_IFNAME`, `GLOO_SOCKET_IFNAME` | `:965,969` | Same, from that node's record | **S** |
| 2.3 | `NCCL_IB_HCA` | `:966` | Same, both twins | **S** |
| 2.4 | `NCCL_IB_DISABLE=0` | `:967` | Same. NCCL's default is already 0 (`NCCL_PARAM(IbDisable, …, 0)`), undocumented in the env guide; setting it explicitly is harmless | **S** |
| 2.5 | `NCCL_IGNORE_CPU_AFFINITY=1` | `:10` | Same, from `vllm.yaml` `runtime.env` | **S** for the value NCCL gives it; **U** for *why on GB10* — see §5.1 |
| 2.6 | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` | `:11` | Same, from `vllm.yaml` | **S** |
| 2.7 | `MN_IF_NAME`, `UCX_NET_DEVICES`, `OMPI_MCA_btl_tcp_if_include`, `TP_SOCKET_IFNAME` | `:963,964,968,970` | Same, and **inert in our launch**. `UCX_NET_DEVICES` is read by libucp on UCX context creation, `OMPI_MCA_*` by Open MPI in `MPI_Init`, `TP_SOCKET_IFNAME` by PyTorch's TensorPipe RPC agent — a `vllm serve` over NCCL and Gloo constructs none of them. Upstream needs them because the same names drive its `mpirun` NCCL tests | **D** — kept for parity. Zero cost, correct if a `ucc` backend is ever selected, and dropping them would be a divergence with no benefit |
| 2.8 | Ray variables (`RAY_NODE_IP_ADDRESS`, `RAY_OVERRIDE_NODE_IP_ADDRESS`, `RAY_memory_monitor_refresh_ms`, `RAY_num_prestart_python_workers`, `RAY_object_store_memory`) | `:961,962,971-973` | Not emitted | **D** — we have no Ray path. `native-runtime-plan.md` appendix A keeps the whole specification |
| 2.9 | Mesh: `NCCL_NET_PLUGIN=none`, `NCCL_IB_SUBNET_AWARE_ROUTING=1` | `autodiscover.sh:188-189`; `NETWORKING.md:444`; **and NVIDIA's own ring playbook exports exactly these two** | `MESH_NCCL_ENV`, emitted per rank when the node's `fabric_mode` is `mesh` | **S** |
| 2.10 | Mesh: `NCCL_IB_MERGE_NICS=0` | `autodiscover.sh:190`. **NVIDIA sets it nowhere** — zero occurrences in the whole playbooks repo | Emitted. We follow the reference | **D, two sources disagree** — see below |
| 2.11 | `GLOO_SOCKET_IFNAME=lo` at one node | Upstream has no size-one path at all | Emitted below two nodes | **S** — Gloo otherwise calls `gethostname()` and resolves it (`ProcessGroupGloo.cpp`, `createDefaultDevice`), warning and falling back to loopback if that fails. `lo` is right only when every rank is on one host, which is exactly the gate |
| 2.12 | SGLang's `--dist-init-addr` at one node | Upstream has no SGLang path. `mark-ramsey-ri/sglang-dgx-spark`, our hardware evidence for SGLang on GB10, passes the node's **RoCE IP** at every size; we pass loopback below two nodes | Loopback | **D** — SGLang reads the flag unconditionally (`model_runner.py`, no `nnodes` guard), and `127.0.0.1:<port>` is behaviourally what it derives when the flag is absent. The one side effect is that supplying it moves the ZMQ ports when `--enable-dp-attention` is on, and we only add that flag above one node — so at one node there is none. Recorded because it is a real difference from the configuration that has actually run |

### 2.10 in full: `NCCL_IB_MERGE_NICS=0` versus subnet-aware routing

Both settings address the same problem and NCCL's source shows them as
alternatives, not companions.

* `NCCL_IB_MERGE_NICS` is documented (NCCL ≥ 2.20): default **1**, "combine
  dual-port IB NICs into a single logical network device… to aggregate
  dual-port NIC bandwidth". The merged device's advertised speed is the
  arithmetic sum of its members (`ncclIbMakeVDeviceInternal`: `mDev->speed +=
  dev->speed`).
* `NCCL_IB_SUBNET_AWARE_ROUTING` is **undocumented** — see §5.2 — but its
  source (`net_ib/connect.cc`, since v2.30.7-1) is explicit that it is the
  finer instrument: it keeps NIC fusion when every port of the fused device can
  reach the peer ("*preserves NIC Fusion bandwidth when both ports connect to
  the same destination*") and un-fuses only per peer when they cannot ("*NIC
  Fusion fused PFs on different subnets… a partial match would leave some QPs
  on PFs with no L2 path to the peer*").

So `MERGE_NICS=0` disables fusion globally and costs aggregation wherever both
ports *do* reach one peer. NVIDIA's ring playbook sets subnet-aware routing and
leaves merging alone; upstream sets both.

**We follow upstream and emit all three**, because `spark-vllm-docker` is the
implementation this work is matching, and in a switchless ring no port reaches
every peer, which is the case where the two settings agree. If a two-node or
switched cluster ever reports mesh cabling, `MERGE_NICS=0` would cost it
bandwidth — but that combination is refused at plan time anyway (§4.6).

---

## 3. The container

Upstream's `docker run` is assembled at `launch-cluster.sh:1075-1108`; the
`native-runtime-plan.md` §1.4 block is its transcription.

| # | Flag | Reference | Spark Pulse | Status |
|---|---|---|---|---|
| 3.1 | `--gpus all -d --network host --name … --entrypoint=` | `:1067,1075` | Same, from `vllm.yaml` `runtime.container` and `ContainerSpec` | **S** |
| 3.2 | `--privileged --ipc=host --ulimit nofile=1048576:1048576` | `:1084-1085` | Same | **S** |
| 3.3 | Non-privileged profile: `--cap-add=IPC_LOCK --shm-size --device=/dev/infiniband --memory --memory-swap --pids-limit` | `:1081-1082` | Same shape, `DockerService.run_container` and the SGLang profile | **S** |
| 3.4 | Cache mounts: HF home, `~/.cache/vllm`, `~/.cache/flashinfer`, `~/.triton`, `~/.tilelang` | `:12,393-406` | Same, `vllm.yaml` `runtime.cache_mounts` | **S** |
| 3.5 | **`mkdir -p` every cache directory before `docker run`** — locally and on each worker over SSH | `:1092-1096`, `:1103-1105` | `NodeService.ensure_directories`, called for every rank's mounts before its container is created | **S** — and see §7.2, this was missing |
| 3.6 | Idle container (`sleep infinity`) then `docker exec` | `:1000,1088` | Same | **S** |
| 3.7 | Serve output redirected to `/proc/1/fd/1` so `docker logs` works | `:1218,1236` | Same, `native_runtime._deploy_script` | **S** |
| 3.8 | Launch script copied to `/workspace/exec-script.sh`, `chmod +x` | `:8,939-953` | Same | **S** |
| 3.9 | Mods: dir or zip with `run.sh`, extracted to `/workspace/mods/<name>`, run with `WORKSPACE_DIR=$PWD` | `:763-825` | Same, `native_runtime._apply_mods`, `WORKSPACE_DIR` = the image workdir | **S** |
| 3.10 | `--rm` on the container | `:1075` | We set `auto_remove=False` | **D** — a rank that crashes must stay inspectable; `docker logs` on a removed container returns nothing, and the gang teardown needs evidence rather than absence |
| 3.11 | `-p/--publish` refused in cluster mode | `:574-577` | Ports are only published when the profile is *not* host-networked, and only on rank 0 | **D** — same outcome by construction rather than by refusal |
| 3.12 | `earlyoom` as the foreground process | `:996-1002` | Not implemented | **D** — an optional upstream convenience, not a cluster semantic. `keepalive` in the engine spec is the hook if it is ever wanted |

---

## 4. Topology, ordering and refusals

| # | Behaviour | Reference | Spark Pulse | Status |
|---|---|---|---|---|
| 4.1 | Required nodes = `tp × pp × dp` | `:1259` | `_check_capacity`, same product | **S** — and vLLM enforces it: above one node `--nnodes` must divide the world size exactly (`engine/arg_utils.py`) |
| 4.2 | More nodes than the parallelism needs ⇒ **trim peers silently** | `:1265-1268` | **Refuse**, naming both numbers | **D** — vLLM raises "must evenly divide the total world size" on every rank, so trimming does not serve on a subset, it fails N containers later than we can say so |
| 4.3 | Fewer nodes than needed ⇒ refuse | `:1262-1264` | Same | **S** |
| 4.4 | Rendezvous flags: `--nnodes N --node-rank R --master-addr HEAD --master-port 29501`, `--headless` above rank 0 | `:903-904`, `:1214,1231` | Identical, at **every** size | **S** — stock `vllm serve` flags since 0.11.1 ([PR #23691](https://github.com/vllm-project/vllm/pull/23691)). At `nnodes=1, dp=1` they are provably unread: `init_distributed_environment` only substitutes `tcp://master_addr:master_port` when `nnodes > 1 or data_parallel_size > 1`, and the executor's `file://` store survives otherwise |
| 4.5 | `--distributed-executor-backend` stripped when not using Ray | `:908,1213,1230` | Always stripped | **S** — with `--nnodes > 1` on CUDA, vLLM resolves the backend to `mp` unconditionally, ahead of any Ray detection (`config/parallel.py`), and refuses `ray` outright above one node |
| 4.6 | Mesh is a property of the **cabling** (four ports up), not of the node count | `autodiscover.sh:121-195` | Same: `fabric_mode` per node, from its own ports. Plus two refusals upstream has no equivalent for — nodes that disagree about their cabling, and a ring at any size other than three | **S** for the ring size (NVIDIA's `nccl/assets/launch.sh` refuses "ring requires exactly 3 nodes"; its Sync assistant routes four nodes to a switch); **D** for the disagreement refusal, which is ours |
| 4.7 | Engine capability gate: 3+ nodes need `capabilities.mesh` | No equivalent | Kept | **D** — stricter than upstream, which gates on nothing. Three nodes behind a switch is legitimate and we refuse it on a `mesh: false` engine. Conservative rather than wrong, and it costs an engine-spec edit to lift |
| 4.8 | Ceiling of four nodes | Upstream has none; `NETWORKING.md:86` points at a switch above two | `MAX_CLUSTER_NODES = 4`, **and a switchless ring is refused at any size but three** | **S** — see the decision below |
| 4.9 | **Create every container first, then launch** — all `docker run`s, then all mods, then the serve command | `:1097-1121` then `:1201-1242` | Same two phases: `_create_rank` for every rank (head first, upstream's order), then `_launch_rank` | **S** — and see §7.3, we used to interleave |
| 4.10 | Launch workers first (background), rank zero last | `:1207-1241` | Same | **U** for *why* — neither vLLM nor SGLang documents a required start order; see §5.4 |
| 4.11 | Teardown: head first, then workers | `:640-646` | Same, `DeployPlan.teardown_order` | **S** for the order upstream uses; the reason (collapse the rendezvous rather than leave workers in a collective timeout) is ours |
| 4.12 | Image identity must match across nodes before anything starts | `:1006-1049` (`docker image inspect --format '{{.Id}}'`, head vs each worker) | Stronger: every node pulls the **digest-pinned** reference the plan resolved, and the pre-flight compares each node's digest to the plan's | **S** |
| 4.13 | Refuse to start when passwordless SSH to a worker fails | `:586-598` | Pre-flight `CHECK_REACHABILITY`; a deploy that skips the pre-flight fails at the first docker command with a typed `SSHError` | **D** — same information, one step later, on the path where an operator chose to skip the check |
| 4.14 | Already-running container ⇒ skip the launch | `:691-713` | Deterministic per-rank names carrying a generation, plus `_reap_earlier_generations`; Docker's atomic name reservation is the idempotency primitive | **D** — `cluster-agent-plan.md` §3.3 |
| 4.15 | Ray head/worker start, `ray status` polling | `:977-994`, `:1166-1186` | Deleted | **D** — never wired into a deploy; specification kept in `native-runtime-plan.md` appendix A |

### 4.8 in full: the four-node decision

The code used to assert that NVIDIA documents a three- **and four**-node
direct-connect mesh. It does not, and the difference matters because it is the
one place the ceiling and the cabling could have let an operator plan a
topology nobody describes.

What NVIDIA actually documents is three arrangements: **two nodes on one
cable**, **three nodes in a switchless ring**
(https://build.nvidia.com/spark/nccl/three-sparks), and **four or more behind
a QSFP switch**. Its Sync cluster assistant supports "two to a maximum of four
DGX Spark devices" and routes four to a switch; its own NCCL launcher aborts
with "ring requires exactly 3 nodes". The only four-node switchless
configuration anyone has published is a community *ring* (not a mesh) that
needs a patched NCCL `LD_PRELOAD`ed into the containers.

**Decided: cap the switchless ring at three, allow four only as a switched
cluster, keep the ceiling at four.** Concretely:

* `MAX_CLUSTER_NODES` stays 4, now citing NVIDIA Sync rather than an invented
  four-node mesh, and its comment names the community eight-node build so a
  reader knows larger clusters exist and that nothing authoritative describes
  configuring one.
* A topology whose nodes report `fabric_mode: mesh` at any size other than
  three is **refused at plan time**, naming NVIDIA's exactly-three rule and
  telling the operator to use three nodes or a switch.
* Four nodes each reporting one cable — which is what a switched cluster looks
  like from `ibdev2netdev` — plan normally and get no ring settings.

The residual gap is a node whose cabling has never been recorded: a switchless
four-node ring whose registry entries carry no `fabric_mode` is not caught at
plan time. The pre-flight catches it, because that node's own `ibdev2netdev`
reports four ports up and the mesh-settings check fires.

---

## 5. Unspecified — the six genuinely unknown

These are the rows where no source consulted answers the question. Each names
what was checked.

### 5.1 Why `NCCL_IGNORE_CPU_AFFINITY=1` on GB10

NCCL documents the variable: by default NCCL uses the intersection of the
inherited CPU affinity and the GPU's; `=1` discards the inherited one and uses
the GPU's alone. What is unspecified is **why a DGX Spark wants it**. Checked:
the NCCL env guide, the DGX Spark playbooks, upstream's own scripts and README.
Upstream sets it at `launch-cluster.sh:10` with no comment. Note the direction
is not obviously the safe one — `=1` trusts the GPU-derived mask, which is the
wrong fix if that mask is the degenerate one. We keep it because the reference
does and because it has run this way on one Spark.

### 5.2 What `NCCL_IB_SUBNET_AWARE_ROUTING` is

**Undocumented by NVIDIA**, and this is a headline result rather than an
aside. Checked: the NCCL env guide for 2.31.2 and its raw reStructuredText
source, the in-repo `docs/userguide/source/env.rst`, and the NCCL release notes
index covering 2.0.2 → 2.31.2. Absent from all four.

Source-level evidence exists and is unambiguous:
`NCCL_PARAM(IbSubnetAwareRouting, "IB_SUBNET_AWARE_ROUTING", 0)` in
`src/transport/net_ib/connect.cc`, **default 0**, introduced in **v2.30.7-1**
(absent at v2.30.4-1). It embeds each port's GID in the connection handle and
picks a local device sharing a subnet with the peer. A companion,
`NCCL_IB_SUBNET_PREFIX_LEN` (default **24**), is equally undocumented — worth
knowing, because a mesh cabled as `/30`s inside one `/24` would not
discriminate. NVIDIA's ring playbook and upstream both set the routing flag to
1 and neither mentions the prefix length.

### 5.3 Whether naming both RoCE twins actually doubles bandwidth

NVIDIA documents that the twins exist ("*`enp1s0f1np1` and `enP2p1s0f1np1`
refer to the same physical port*", `connect-two-sparks/README.md`) and
**publishes no `NCCL_IB_HCA` value for DGX Spark at all** — its own NCCL
launcher sets only `NCCL_SOCKET_IFNAME` and comments that "*NCCL discovers them
— no need to name them*". A repo-wide grep of `dgx-spark-playbooks` for
`NCCL_IB_HCA` returns only DGX **Station** hits (`mlx5_0,mlx5_1`).

What NVIDIA *does* publish is the measurement underneath the rule: two
simultaneous `ib_write_bw` runs on the two twins of one port give **92.57 +
97.28 = 189.85 Gbps**, against a ~184 Gbit/s acceptance threshold for a healthy
link. So the aggregation is real at the perftest level. Whether NCCL reaches it
by being handed both device names is the unproven part, and NCCL's own model
cuts the other way: its merged-device speed is arithmetic (`mDev->speed +=
dev->speed`) and it has no representation of two devices sharing one wire, so
it could over-report and mis-plan.

**Two sources disagree and we follow upstream** (name both), because it is the
sanctioned reference and because NVIDIA's alternative — name none — is a
different design, not a correction of this one.

### 5.4 Which process must start first

**Neither engine documents a start order.** Checked: vLLM's
`docs/serving/parallelism_scaling.md`, which presents head and worker commands
in sequence without any sequencing language; SGLang's multi-node deployment
doc, whose SLURM example starts every rank *simultaneously* under `srun`.

Mechanically both converge on the same shape — rank 0 owns the store and
non-zero ranks are TCPStore clients that retry until the `init_process_group`
timeout — so workers started first block rather than fail. But that is a
PyTorch TCPStore property, not an engine guarantee. Upstream starts workers
first (`launch-cluster.sh:1207-1241`) and so do we; **it is our policy, not a
documented requirement.**

### 5.5 Whether a mesh really sustains only 100G per pair

`NETWORKING.md:43` says a three-Spark daisy chain sustains 100G between each
pair. Checked: NVIDIA publishes **no bandwidth figure for the ring at all**,
and its ring playbook states the opposite in wording — "*Each CX7 port provides
full 200GbE bandwidth*". The mechanism is consistent with NVIDIA's own perftest
data (in a ring each link uses one port, hence one twin pair), but the number
is a single community claim. We repeat it in a refusal message and attribute it
there.

### 5.6 SGLang's `--enable-dp-attention` as a cross-node workaround

`native-runtime-plan.md` §1.5 records it, sourced from
`mark-ramsey-ri/sglang-dgx-spark`, as a workaround for FlashInfer's all-reduce
fusion failing across nodes with "invalid device context". Checked: SGLang
issues, PRs, docs and source at `v0.5.10.post1` for that error string —
**no occurrence anywhere**. The source shows SGLang already handling the case
itself: custom all-reduce self-disables across nodes with a warning
(`custom_all_reduce_utils.py`), and FlashInfer fusion is not auto-enabled when
`nnodes != 1` (`server_args.py`). SGLang's *own* verified DGX Spark two-node
cookbook entry ([PR #33131](https://github.com/sgl-project/sglang/pull/33131))
uses neither `--enable-dp-attention` nor `--disable-cuda-graph`.

`sglang.yaml` still declares `multi_node.extra_args: [--enable-dp-attention]`.
**This is left as it is deliberately**: changing an engine default on the
strength of one community source contradicting another, with no hardware to
settle it, is the guessing this document exists to avoid. It is recorded here
so a second Spark settles it rather than inherits it.

---

## 6. Corrections to claims we were making

Five statements in the code and the plan documents were wrong and are now
fixed. Every one of them was wrong in the direction of sounding more certain
than the evidence.

**6.1 `NCCL_IB_HCA` is not find-or-fail.** The pre-flight and the engine both
said interface pinning is find-or-fail and a wrong name aborts the collective.
That is true of `NCCL_SOCKET_IFNAME` — NCCL's selector literally comments
`// Specified by user : find or fail` and its callers `WARN("Bootstrap : no
socket interface found")` — and **false of `NCCL_IB_HCA`**. A selector matching
no device leaves NCCL with zero IB devices, which disables the IB plugin and
falls through to TCP sockets at INFO level (`plugin/net.cc`). The deployment
comes up, serves, and runs at a fraction of the fabric's bandwidth with nothing
in the log. That is worse than an abort, and the remedy text now says so.

**6.2 A trimmed topology does not hang.** The refusal for a parallelism smaller
than the node count said the launch "would hang until the 600 s rendezvous
timeout". vLLM refuses first: above one node it requires `--nnodes` to divide
the world size exactly and raises "must evenly divide the total world size"
during argument validation. The refusal is still right; its reason is now
accurate.

**6.3 There is no torchrun in this path, and NCCL has no collective timeout.**
`cluster-agent-plan.md` §3.3 set timings against "torchrun waits 600 seconds
for rendezvous and NCCL waits 600 seconds on a collective". Both halves are
wrong. vLLM's `--nnodes` path is the `mp` executor — torchrun is only relevant
to `--distributed-executor-backend external_launcher`. And NCCL has no
collective timeout and no environment variable for one: the 600 s is PyTorch's
default `init_process_group` timeout for NCCL (1800 s for gloo), settable only
through `timeout=`, which vLLM exposes as `--distributed-timeout-seconds` and
otherwise leaves alone. vLLM separately waits `VLLM_ENGINE_READY_TIMEOUT_S`,
also 600 s. The number the plan's timing table is built on survives; the
attribution did not. Corrected in `cluster-agent-plan.md` §3.3 and in the four
places `native_runtime.py` repeated it.

**6.4 "Provably never read" was too strong for the rendezvous flags.**
`vllm.py` said vLLM "derives a file-based store rather than a TCP one below
two nodes", which reads as though the node count chooses the store. It does
not: the executor picks a `file://` store **unconditionally**, and
`init_distributed_environment` overrides it with
`tcp://master_addr:master_port` only when `nnodes > 1` **or**
`data_parallel_size > 1`. The conclusion — render the flags at every size —
survives; the argument did not, and `--master-addr` really is read at
`nnodes=1` with `dp>1`, where `create_engine_config` seeds the data-parallel
address from it. We render loopback there, which is the value that path would
have taken anyway. The comment now says exactly this.

**6.5 A comment claimed a fabric was confirmed on our Spark.**
`discovery.ibdev2netdev_up` said the tool was "confirmed present on the
Spark", which is true and reads as more than it is. Checked this session: the
binary is at `/usr/sbin/ibdev2netdev`, and that machine has an empty
`/sys/class/infiniband`, no `15b3` vendor id anywhere on the PCI bus and no
CX7 netdevs — its links are `enP7s7`, `wlP9s9`, `docker0` and `lo`. So the
tool has never printed a port line for us. Every rule built on its output is
written against `NETWORKING.md` lines 22-27 and NVIDIA's playbooks and is
exercised only against those samples. The docstring says so now.

---

## 7. Defects this work found in our own code

**7.1 `NCCL_IB_HCA` would have been empty on a real Spark.** The registry's
`infiniband_interfaces` was filled by `_classify_interface`, which calls a
netdev "infiniband" when its name starts with `ib` or `mlx5`. On a DGX Spark
the RoCE devices are `rocep1s0f1` and live in `/sys/class/infiniband`, while
the netdevs they drive are `enp1s0f1np1` — classified as plain ethernet. So the
scan found **no fabric at all**, the record held nothing, and a multi-node
launch would have pinned no RoCE device. The pre-flight was already checking
`NCCL_IB_HCA` names against `/sys/class/infiniband`, so the field's intended
contents were never in doubt — only its contents. Discovery now reads
`ibdev2netdev`.

**7.2 `NCCL_IB_HCA` would also have named one twin, not two**, even had the
above worked: `build_nccl_defaults` took `devices[0].hca` and stopped. Every
active device is named now.

**7.3 The management interface was picked by scan order.** `run_discovery` and
`_discovered_self` took the first up ethernet with an IP. On a Spark that is
whichever of `enP7s7`, `wlP9s9` and the fabric twins psutil happened to
enumerate first. Upstream's rules — the addressed non-`P` twin for one cable,
`enP7s7` for a mesh — now decide, with the scan as the fallback for machines
that have no fabric.

**7.4 Cache mount sources were never created.** Upstream `mkdir -p`s them on
every node before `docker run` (`:1094`, `:1104`). We did not, so Docker
created any missing bind source **owned by root** — which is exactly the
condition `AGENT_RUNBOOK.md:310-324` devotes a section to repairing. Note that
we inherit upstream's other limitation here untouched: the paths are expanded
on the control node, so a peer with a different login user gets the control
node's home path created under its own `$HOME`'s absence. Upstream has the same
behaviour (`-v $HOME/.cache/vllm` is computed on the head and passed verbatim
over ssh) and we did not invent a fix for it.

**7.5 Containers were created and launched in one pass per rank.** Rank one
could be serving and at the rendezvous before rank zero's container was created
— so an image missing on rank zero surfaced *after* a worker had started
waiting. Upstream creates every container, applies every mod, and only then
runs the serve command. We do the same now, and the teardown on failure is
correspondingly earlier: a create-phase failure tears down idle containers with
nothing launched anywhere.

**7.6 The refusal text called three and four nodes "a direct-connect mesh".**
Four nodes are not: NVIDIA routes four to a switch and its NCCL launcher
refuses a ring at any size but three. The text is corrected and a plan-time
refusal now enforces it — see §4.8 for the decision.

**7.7 One end-to-end spec raced another file.** `nodes.spec.ts`'s listing test
read `/api/nodes`, loaded the page, and asserted every node it had seen was
rendered. `multinode.spec.ts` enrols three nodes and forgets them again, in a
different worker against the same backend, so a node could vanish between the
read and the render. The file's `mode: "serial"` says it is guarding against
exactly this and only reaches inside one file. The listing test now asserts
against the nodes the backend held both before *and* after the render, which
is the property it was always meant to check: the page shows what the backend
holds, not what it briefly held. Unrelated to the parity work, found because
the gate failed two runs in three.

**7.8 The pre-flight could not read a valid `NCCL_IB_HCA`.** It split the
value on commas and compared the tokens to `/sys/class/infiniband`, so
`=rocep1s0f1:1` failed, `^=mlx5_1` was reported as a missing device rather
than an exclusion, and a prefix selector such as `mlx5` failed even with four
matching devices present. `selector_names` and `_resolves` now implement the
documented grammar. We never *generate* those forms, but an operator can type
one into a registry record or a recipe's env block, and a pre-flight that
refuses a legal value is worse than none.

---

## 8. What a second Spark would settle

Split by the taxonomy above, because these carry different risk.

**Specified, awaiting confirmation only** — the source says what to do, we did
it, and hardware would confirm it works:

* the rendezvous forming across machines, for either engine (4.4);
* interface pinning against real per-role names (1.2, 1.5, 1.6, 7.1–7.3);
* both RoCE twins reaching NVIDIA's ~190 Gbps aggregate (5.3, and the perftest
  number is NVIDIA's own);
* the ring's two NVIDIA-published NCCL settings behaving as documented (2.9);
* an unreachable peer over a real SSH transport rather than a simulated one.

**Unspecified, where hardware would discover the answer rather than confirm
it** — nobody documents these and a measurement is the only way to know:

* whether `NCCL_IB_MERGE_NICS=0` helps or costs on this fabric (2.10);
* whether workers-first ordering matters at all (4.10, 5.4);
* whether a ring really sustains 100G per pair (5.5);
* whether SGLang needs `--enable-dp-attention` across nodes (5.6);
* whether `NCCL_IGNORE_CPU_AFFINITY=1` is the right direction on GB10 (5.1).

`web/src/lib/experimental.ts` renders this split to the operator, and the two
must stay in step.
