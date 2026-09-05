# Cluster evidence

Multi-node is implemented and has never been run on two machines. This document
is the substitute for running it: for each decision we cannot test, what the
authoritative source says, what we do, and whether they agree.

Every claim carries a citation — a URL, a file and line in this repository, or a
file and line in `eugr/spark-vllm-docker` at `358bf26` (read on the Spark at
`~/projects/spark-vllm-docker`). Nothing here is reasoning presented as
evidence. Where a source contradicts our code, section 0 says so first.

Findings are classified:

* **Specified** — a source settles it.
* **Contested** — sources disagree; the entry names which we follow and why.
* **Unspecified** — nobody documents it; the entry names the sources checked
  and what they failed to answer.

Researched 2026-09-04. Versions: NCCL User Guide 2.31.2; NCCL source at
`fd16832` (the ref `spark_pulse/engines/defaults/vllm.yaml:19` pins);
vLLM `main` @ `3284af6`; SGLang `v0.5.10.post1` and `main`; PyTorch `main`.

---

## 0. Where a source contradicts us

Fourteen, worst first. Each is expanded in the numbered section named.

### 0.1 We can never populate `NCCL_IB_HCA` on a DGX Spark  (§1, §2)

`spark_pulse/tools/node_registry.py:388-392` derives a node's fabric devices by
filtering `discovery.detect_network_interfaces()` for `type == "infiniband"`.
That classifier is `spark_pulse/tools/discovery.py:173-174`:

```python
if name.startswith(("ib", "mlx5")):
    return "infiniband"
```

and it runs over **net devices** — `psutil.net_if_addrs()`
(`discovery.py:196`) or `/sys/class/net` (`discovery.py:231`).

On DGX Spark the RoCE devices are named `rocep1s0f0`, `rocep1s0f1`,
`roceP2p1s0f0`, `roceP2p1s0f1` (upstream `docs/NETWORKING.md:22-27`; NVIDIA's
[DGX Spark clustering guide](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
gives the same correspondence table). None of them starts with `ib` or `mlx5`,
and none of them is a net device — they live in `/sys/class/infiniband`, and
`ibdev2netdev` exists precisely to map them onto the `enp*`/`enP*` net devices.

So `_discovered_self()` returns an empty `infiniband_interfaces` on every DGX
Spark, `native_runtime.py:820` joins an empty tuple, `_fabric_env`
(`engines/base.py:474-475`, `engines/vllm.py:125-126`) sees a falsy `ib_if`, and
**`NCCL_IB_HCA` is never emitted**. The one variable upstream says is required
for full bandwidth (`docs/NETWORKING.md:38`) cannot be set except by hand
through `PATCH /api/nodes/{id}`. `plan` does warn
(`native_runtime.py:824-830`), but as a soft warning about NCCL "choosing a link
itself", not as the specific fact that the fabric selector is absent.

We already have the right primitives and do not use them here:
`discovery.detect_infiniband_devices()` scans `/sys/class/infiniband`
(`discovery.py:367-371`) and `discovery.ibdev2netdev_up()` parses
`ibdev2netdev` (`discovery.py:677-702`).

Two collateral notes. `discovery.py:730-732` filters interfaces classified
`infiniband` against `ibdev2netdev`'s **net-device** column; since a name that
classifies as `infiniband` is `ib*`/`mlx5*` and that column holds `enp*`, the
comparison cannot match on a Spark either. And `discovery.py:680` states
`ibdev2netdev` is "confirmed present on the Spark": it is present
(`/usr/sbin/ibdev2netdev`) but on the machine at `192.168.29.60` it prints
nothing and `/sys/class/infiniband` does not exist, so the CX7 stack is not
loaded there. That machine cannot self-register a fabric interface under any
implementation.

### 0.2 The vLLM "provably never read" claim is refuted  (§7)

`spark_pulse/engines/vllm.py:15-18`:

> "at one node they are provably never read: vLLM derives a file-based store
> rather than a TCP one below two nodes"

Both halves are wrong.

*The mechanism is not node-count-conditioned.* `MultiprocExecutor` picks the
file store **unconditionally**, the only exception being ROCm/AITER —
[`vllm/v1/executor/multiproc_executor.py:138-143`](https://github.com/vllm-project/vllm/blob/3284af6bf1be8429c332bd5fafba579c2d7557da/vllm/v1/executor/multiproc_executor.py#L138-L143).
The node count is consulted downstream as an *override*, in
[`vllm/distributed/parallel_state.py:1766-1796`](https://github.com/vllm-project/vllm/blob/3284af6bf1be8429c332bd5fafba579c2d7557da/vllm/distributed/parallel_state.py#L1766-L1796),
whose condition is `nnodes > 1 **or** data_parallel_size > 1`. One node with
`--data-parallel-size 2` therefore rendezvouses over a **TCPStore**, not a file
store.

*It also did not exist in 0.11.1*, the version the same docstring names
(`vllm.py:96-99`, `min_framework_version = (0, 11, 1)`). The file store arrived
in PR #50999, commit `11ba93f36` (2026-08-10), first released in **v0.28.0**.
In v0.11.1, `multiproc_executor.py:127-128` built a TCP rendezvous on loopback
unconditionally. Our shipped image is `framework_version: "0.28.1"`
(`engines/defaults/vllm.yaml:10`), so the store exists for us — but the
docstring's stated justification is nine months and four minor versions out of
date relative to the floor it declares.

*"Never read" is false for `--master-addr`.*
[`vllm/engine/arg_utils.py:2253-2267`](https://github.com/vllm-project/vllm/blob/3284af6bf1be8429c332bd5fafba579c2d7557da/vllm/engine/arg_utils.py#L2253-L2267)
reads it with no `nnodes` guard at all, into `data_parallel_master_ip`. Only
`--master-port` is genuinely guarded (`parallel_state.py:1783-1785`).
`--nnodes` and `--node-rank` are read and then reduce to no-ops
(`vllm/config/parallel.py:742-748`).

**The conclusion survives; the argument does not.** Rendering the flags at every
size is still safe, because we render `--master-addr 127.0.0.1` at size one
(`engines/base.py:212-217`) which is also vLLM's own default
(`vllm/config/parallel.py:277-288`), and we never render
`--data-parallel-size`. But "provably never read" should become "read, and
inert at one node with data parallel of one" — and a v1 recipe `command:`
template carrying `-dp 2` would make `--master-addr` live at size one. Upstream
parses exactly that flag (`launch-cluster.sh:852`).

### 0.3 NVIDIA publishes no four-node direct-connect mesh  (§6)

`spark_pulse/engines/base.py:24-25`:

> "NVIDIA publishes a two-node bring-up guide for DGX Spark and a three- and
> four-node direct-connect mesh, and nothing at all above four."

NVIDIA publishes a two-node direct guide and a **three**-node direct ring. Four
nodes are documented only **through a switch**. The
[Sync Cluster Assistant documentation](https://docs.nvidia.com/sync/latest/cluster-assistant.html)
says direct connect "does not support four devices", and NVIDIA's own
configuration script aborts:

> "Detected a ring/line-style point-to-point topology with 4 or more machines.
> This configuration is NOT supported by this script yet. Aborting."
> — [`detect_and_configure_cluster_networking.py`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/multi-sparks-through-switch/assets/spark_cluster_setup/node_scripts/detect_and_configure_cluster_networking.py)

A four-node *full* mesh is anyway impossible with two ports per node. Upstream
agrees: `docs/NETWORKING.md:84-87` says more than 2 Sparks needs a switch, and
its mesh section (`:45-82`) and mesh netplan (`:156-253`) cover three nodes
only. `autodiscover.sh:193` refuses anything but 2 or 4 *up CX7 interfaces*,
which is a per-node port count, not a node count.

Consequence: `MAX_CLUSTER_NODES = 4` (`base.py:29`) with
`capabilities.mesh: true` on vLLM (`engines/defaults/vllm.yaml:70`) lets an
operator plan a four-node direct deployment that no source describes.
`supports_size` (`base.py:431-440`) distinguishes only "≥3 needs mesh"; it does
not distinguish three from four, nor direct from switched.

### 0.4 The ten-minute timeout is PyTorch's, not NCCL's  (§4)

We call it "the ten-minute NCCL collective timeout" in five places:
`spark_pulse/tools/native_runtime.py:22`, `native_runtime.py:1390`,
`web/src/lib/experimental.ts:27`, `docs/cluster-agent-plan.md:289` ("NCCL waits
600 seconds on a collective") and `docs/cluster-agent-plan.md:308`.

**NCCL has no collective timeout.** `grep -r NCCL_TIMEOUT` over the NCCL source
tree returns nothing, and the variable is absent from
[NCCL's environment variable reference](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html).
NCCL's only timeouts are transport-level: `NCCL_IB_TIMEOUT` (default 20, i.e.
4.096 µs × 2²⁰ ≈ 4.3 s per attempt) and the socket retry budget
(`NCCL_SOCKET_RETRY_CNT` 34 × `NCCL_SOCKET_RETRY_SLEEP_MSEC` 100 ≈ 60 s).

Ten minutes is **PyTorch's** `ProcessGroupNCCL` default —
[`torch/csrc/distributed/c10d/ProcessGroupNCCL.hpp:158-159`](https://github.com/pytorch/pytorch/blob/main/torch/csrc/distributed/c10d/ProcessGroupNCCL.hpp#L158-L159):

```cpp
constexpr auto kProcessGroupNCCLDefaultTimeout =
    std::chrono::milliseconds(10 * 60 * 1000);
```

surfaced as the `timeout=` default of `init_process_group` ("Default value is 10
minutes for NCCL and 30 minutes for other backends"). The number is right; the
owner is wrong, and it matters because the fix is a `timeout=` argument, not an
environment variable — see 0.5.

`docs/cluster-agent-plan.md:288` ("torchrun waits 600 seconds for rendezvous")
is numerically correct — `RendezvousTimeout._DEFAULT_TIMEOUTS["join"] =
timedelta(seconds=600)` — but neither engine path we render goes through
torchrun. vLLM uses its own `MultiprocExecutor`; SGLang calls
`init_process_group` directly.

### 0.5 `NCCL_TIMEOUT=1200000` is inert  (§4)

`spark_pulse/engines/defaults/sglang.yaml:33` sets it, copied from
`mark-ramsey-ri/sglang-dgx-spark` `config.env` and recorded in
`docs/native-runtime-plan.md:137`. NCCL does not read it (see 0.4). It is set at
**every** size, including solo, via `base_env`'s
`env.update(self.spec.runtime.env)` (`engines/base.py:487`).

SGLang's actual knob is `--dist-timeout` (seconds), plumbed into
`init_process_group(timeout=…)`. Neither we nor the source repo sets it, so
PyTorch's 10-minute default applies and the intended 20 minutes is not in
effect.

### 0.6 `NCCL_NET_GDR_LEVEL=5` is out of range  (§3)

`spark_pulse/engines/defaults/sglang.yaml:32`. NCCL documents the string form
`LOC`/`PIX`/`PXB`/`PHB`/`SYS` and describes the integer form as legacy,
"discouraged due to breaking changes in path types", with legacy values 0–4.
`5` is outside that range. Inherited from the same `config.env`.

### 0.7 Our preflight applies find-or-fail to the wrong variable  (§2)

`spark_pulse/tools/preflight.py:1214-1219` fails a check when a named
InfiniBand device is missing, with the message "Interface pinning is
find-or-fail: the collective aborts on {node} rather than picking another link."

That is exactly right for `NCCL_SOCKET_IFNAME` and exactly wrong for
`NCCL_IB_HCA`. The two behave differently and the asymmetry is documented
nowhere:

* `NCCL_SOCKET_IFNAME` — `src/misc/socket.cc:200` carries the literal comment
  `// Specified by user : find or fail`; the fallback cascade exists only in the
  `else` branch, and the callers turn zero matches into a hard error
  (`src/bootstrap.cc:124-128`, `ncclInvalidUsage`).
* `NCCL_IB_HCA` — a bogus value is **not** fatal.
  `src/transport/net_ib/init.cc:474-476` logs `NET/IB : No device found.`, and
  `src/plugin/net.cc:181` then disables the IB plugin, so NCCL falls through to
  the socket transport. A typo silently costs you RDMA rather than failing the
  launch.

Our docstrings inherit the error: `engines/base.py:450-452` and
`preflight.py:1148-1152` both attribute find-or-fail to the pair
`NCCL_SOCKET_IFNAME`/`GLOO_SOCKET_IFNAME`, which is correct, but the check that
consumes them treats IB devices identically.

Failing the launch on a missing IB device is arguably the better behaviour —
silent socket fallback is a 10–20× slowdown, per
`docs/native-runtime-plan.md:152`. The message just should not claim NCCL does
it.

### 0.8 Our preflight cannot parse the documented `NCCL_IB_HCA` syntax  (§1)

`preflight.py:1243-1245` splits `NCCL_IB_HCA` on commas and
`preflight.py:1192` requires each token to be a literal entry in
`/sys/class/infiniband`. NCCL's documented grammar is
`[^][=]<hca>[:<port>[:<rail>[:<plane>]]]`, comma-separated. A perfectly valid
`=rocep1s0f1:1,roceP2p1s0f1:1` or `^=rocep1s0f0` would be reported as a missing
device and fail the pre-flight. We never *generate* those forms
(`native_runtime.py:820` joins bare names), but an operator can set them through
`PATCH /api/nodes/{id}`.

### 0.9 Nothing emits the mesh-specific NCCL settings  (§6)

Both upstream and NVIDIA require three extra variables for the three-node ring:

* upstream `autodiscover.sh:188-190` exports `NCCL_NET_PLUGIN=none`,
  `NCCL_IB_SUBNET_AWARE_ROUTING=1`, `NCCL_IB_MERGE_NICS=0`; the same three
  appear in the mesh NCCL test at `docs/NETWORKING.md:444`;
  `docs/native-runtime-plan.md:100` records them as part of our contract.
* NVIDIA's [`nvidia/nccl` playbook](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/nccl/README.md)
  labels `NCCL_IB_SUBNET_AWARE_ROUTING=1` and `NCCL_NET_PLUGIN=none`
  "Ring-specific NCCL settings"; its `spark_cluster_setup.py` adds
  `-x NCCL_IB_MERGE_NICS=0 -x NCCL_NET_PLUGIN=none` when
  `ring_topology = (len(nodes_info) == 3 and len(up_interfaces) == 4)`.

`grep -rn "NCCL_NET_PLUGIN\|SUBNET_AWARE\|MERGE_NICS" spark_pulse/` returns
nothing. `pinning_env` (`engines/base.py:464-466`) branches only on
`node_count > 1`; there is no `>= 3` branch anywhere. vLLM nevertheless declares
`mesh: true` (`engines/defaults/vllm.yaml:70`), so a three-node plan is accepted
and rendered without them.

`NCCL_IB_MERGE_NICS` defaults to `1` (`src/transport/net_ib/init.cc:29`), so
the omission is not neutral: the mesh case needs it explicitly off.

### 0.10 Mesh needs the 10G management link, and we have no size-aware choice  (§6)

Upstream selects `ETH_IF` differently by topology:

* two nodes — the QSFP net device *without* a capital `P`, i.e.
  `enp1s0f1np1` (`autodiscover.sh:132-158`);
* mesh — `enP7s7`, the 10G RJ-45, falling back to `wlP9s9`
  (`autodiscover.sh:172-184`), with `docs/NETWORKING.md:434` stating "For
  3-node mesh we have to use 10G interface for OOB communication!".

NVIDIA's `nvidia/nccl` playbook goes further and pins `NCCL_SOCKET_IFNAME=enP7s7`
for the **two**-node case as well.

Our registry carries a single `ethernet_interface` per node
(`node_registry.py:122`) chosen as the first up ethernet with an IP
(`node_registry.py:383-386`), with no topology awareness and no defined ordering
— on a Spark with both the QSFP link and `enP7s7` up, which one wins is whatever
`psutil` enumerates first.

### 0.11 The different-subnet rule is stated, enforced upstream, unchecked by us  (§5)

Upstream enforces it programmatically — `autodiscover.sh:103-117` builds a
`SEEN_SUBNETS` map and errors with "Interfaces X and Y share the same subnet" —
and states it in prose at `docs/NETWORKING.md:133`.

`spark_pulse/tools/preflight.py` has no subnet check (`CHECK_*` constants at
`preflight.py:82-90`). We only *mention* the rule in the remediation text of a
failed interface check (`preflight.py:1217-1219`) and in the unproven list
(`web/src/lib/experimental.ts:26`). We never gather addresses, so we could not
check it today.

### 0.12 SGLang at size one: we set what SGLang's docs omit  (§8)

`spark_pulse/engines/sglang.py:100-113` renders
`--dist-init-addr 127.0.0.1:50000` at size one, justified at `sglang.py:8-10`.

The rendezvous half is right: SGLang reads the address unconditionally at
`--nnodes 1` (no loopback fallback once set), so loopback is a working and
robust value. But SGLang's own default is `None` (`server_args.py:921-928`), its
documented single-node invocation passes no `--dist-init-addr` at all, and
several modules use `dist_init_addr is not None` as the *test for being
multi-node*:

> "This PR forces `dist_init_addr` to `None` during single-node deployments.
> ### Rationale — In the current implementation, several components rely on the
> condition `dist_init_addr is not None` to identify multi-node environments.
> Allowing a non-null address in a single-node setup leads to incorrect
> environment detection, causing issues such as the connectivity errors reported
> in #22877."
> — [SGLang PR #23158](https://github.com/sgl-project/sglang/pull/23158)

That PR was closed as stale, not merged, so the hazard is live in the image we
ship. The pattern it describes is still in `main`
(`disaggregation/common/conn.py:793-796`, `# Multi-node case: bootstrap
server's host is dist_init_addr`). Setting loopback avoids pointing a
"multi-node" branch at a fabric address, which is the milder failure — but
omitting the flag at size one is what the source project documents.

Our justification "SGLang honours `--dist-init-addr` at one node too"
(`sglang.py:8`) is **correct and now cited**; the choice to set it rather than
omit it is the part no source supports.

### 0.13 `NCCL_IB_DISABLE=0` is a documented no-op  (§3)

`spark_pulse/engines/vllm.py:111` and `engines/defaults/sglang.yaml:31` set it
at every size. NCCL's default is already 0 (`src/transport/net_ib/init.cc:27`,
`NCCL_PARAM(IbDisable, "IB_DISABLE", 0)`) and its sole use site is
`if (ncclParamIbDisable()) return ncclInternalError;` (`init.cc:291`). Setting
it to 0 does not force, require or verify InfiniBand; if RoCE is unavailable
NCCL still falls back to sockets. Harmless, and copied faithfully from
`launch-cluster.sh:967` — but it should not be read as "we required RDMA".

### 0.14 Upstream's bandwidth-aggregation claim has no vendor documentation  (§1)

`docs/NETWORKING.md:38` asserts that naming both RoCE twins is what delivers
full bandwidth, and `native_runtime.py:818-819` reproduces the mechanism with
the more careful word "selector list". NCCL's documentation calls
`NCCL_IB_HCA` a **filter** and never says that naming N devices makes N carry
traffic. See §1 — the claim is *contested*, not false, and we are following the
right source for this hardware; the docstring's caution is warranted and the
phrasing at `web/src/lib/experimental.ts:25` ("whether the twin-adapter
configuration reaches NVIDIA's throughput threshold") is exactly the right
scepticism.

---

## Fabric and NCCL

### 1. `NCCL_IB_HCA` — syntax, and whether naming both twins aggregates

**Syntax: specified.**
[NCCL env reference](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html#nccl-ib-hca):

> "Define to **filter** IB Verbs interfaces to be used by NCCL. The list is
> comma-separated; each entry follows the form
> `<hca>[:<port>[:<rail>[:<plane>]]]` … An optional prefix `^` indicates the
> list is an exclude list. A second optional prefix `=` indicates that the
> tokens are exact names, otherwise by default NCCL would treat each token as a
> prefix. … When `<port>` is omitted, all ports on the HCA are used."

Documented examples: `=mlx5_0:1,mlx5_1:1` = "Use ports 1 of cards `mlx5_0` and
`mlx5_1`"; `^=mlx5_1,mlx5_4` = exclusion. So `mlx5_0:1` means **HCA `mlx5_0`,
port 1 only**; omitting the port means all ports of that card. `^` and `=` are
global prefixes on the whole list, stripped once before tokenising
(`src/os/linux.cc:385-393`). Warning worth heeding: "using `mlx5_1` without a
preceding `=` will select `mlx5_1` as well as `mlx5_10` to `mlx5_19`". There is
a hard limit of 32 HCAs.

**Aggregation: contested.** NCCL's documentation never states that naming N
HCAs causes N to carry traffic; the word it uses is "filter". In the source,
every match becomes a separate net device
(`src/transport/net_ib/init.cc:356-460`), but the graph search then assigns nets
to channels round-robin (`src/graph/search.cc:751-758`) and is free to converge
on fewer. NCCL maintainers are explicit that this is a permission, not a
directive:

> "NCCL is able to aggregate the bandwidth of multiple NICs, but that requires
> GPUs to be connected with NVLink with a bandwidth that matches or exceeds the
> sum of the bandwidth of all NICs."
> — [nccl#412](https://github.com/NVIDIA/nccl/issues/412)
>
> "NCCL keeps the number of NICs used to a minimum as it also conditions GPU SM
> usage." — [nccl#981](https://github.com/NVIDIA/nccl/issues/981)

`NCCL_CROSS_NIC` is the closest documentation gets to confirming concurrency
("controls whether NCCL should allow rings/trees to use different NICs … This
has no effect on systems with only one NIC"). `NCCL_IB_MERGE_NICS` is about a
different thing — "combine **dual-port IB NICs** into a single logical network
device … to more easily aggregate dual-port NIC bandwidth" — and defaults to 1.

**On this hardware specifically**, the twins are not two cards but two PCIe x4
halves of one ConnectX-7 in multi-host mode. NVIDIA employee `isdias` on
[the ConnectX-7 thread](https://forums.developer.nvidia.com/t/connectx-7-nic-in-dgx-spark/350417):

> "in order to achieve the 200gbps speed, we had to use the Cx7's multi-host
> mode, aggregating 2 separate x4-wide PCIe links" (post 2)
>
> "NCCL should be able to sort things out across multiple links … Aggregation
> should be built between the halves … NCCL is topology aware and will figure it
> all out." (post 65)

and community measurement rules out the alternative: Linux bonding does *not*
work (`eugr` post 47, "Creating a bond in XOR doesn't let you have 200G on a
single IP, and LACP bond doesn't work"; `AndrewMyers` post 41, "Linux bonding
won't aggregate a single NCCL collective … What works reliably is no bond,
multi-rail"). NVIDIA's own `spark_cluster_setup.py` passes
`-x NCCL_IB_HCA=rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1` — all four
devices — though its *documented* `nvidia/nccl` playbook sets no `NCCL_IB_HCA`
at all.

**Verdict.** Upstream's assertion (`docs/NETWORKING.md:38`) is unsupported by
NCCL's documentation, supported by NVIDIA-employee forum prose and by NVIDIA's
own script, and consistent with the only mechanism anyone reports working. We
follow it. `native_runtime.py:818-820` produces exactly upstream's form, and its
comment's word "selector" is the accurate one.

### 2. `NCCL_SOCKET_IFNAME` — lists, prefixes, `^`, and the missing-interface case

**Syntax: specified.**
[Env reference](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html#nccl-socket-ifname):
a comma-separated list of **prefixes**; `^` excludes; `=` forces exact match.
`eth` matches `eth0`, `eth1`, …; `=eth0` matches only `eth0`; `^docker`
excludes anything starting `docker`. Prefix matching confirmed at
`src/misc/utils.cc:218-222`. "Setting NCCL_SOCKET_IFNAME will bypass the
automatic interface selection algorithm."

**Find-or-fail: specified, but only by the source.** The documentation never
says what happens when a named interface is absent. `src/misc/socket.cc:190-231`
does, in a comment at line 200: `// Specified by user : find or fail`. The
auto-selection cascade (`ib` → `NCCL_COMM_ID` subnet → `^docker,lo,virbr` →
`docker` → `lo` → `virbr`) exists only in the `else` branch. Callers turn zero
matches into hard errors: `src/bootstrap.cc:124-128` warns "Bootstrap : no
socket interface found" and returns `ncclInvalidUsage`;
`src/transport/net_socket.cc:62-67` returns `ncclInternalError`.

**So our description is correct** — `engines/base.py:450-452`,
`preflight.py:1148-1152`, `native_runtime.py:802-804`,
`docs/cluster-agent-plan.md:527-531`. It does **not** extend to `NCCL_IB_HCA`
(see 0.7).

Three source-only caveats we do not currently account for:

* `src/misc/socket.cc:192` guards on `strlen(env) > 1`, so a one-character
  value is silently ignored and full auto-detection runs instead.
* `matchIfList` returns true when the parsed list is empty
  (`src/misc/utils.cc:231-234`), so a value that tokenises to nothing matches
  everything.
* Only `IFF_RUNNING` interfaces are candidates (`src/os/linux.cc:401-403`): an
  interface that exists but is down is, for this purpose, absent. Our pre-flight
  checks existence in `/sys/class/net` (`preflight.py:499-500`), not link state.

`GLOO_SOCKET_IFNAME=lo` at size one (`engines/base.py:459-466`) is a Gloo
variable, not an NCCL one, and NCCL's find-or-fail says nothing about it. The
docstring's rationale — that Gloo otherwise resolves the hostname — is
**unspecified** here: I did not find a PyTorch document stating that fallback.
It is a plausible and harmless setting; it is not a cited one.

### 3. `NCCL_IB_DISABLE` and `NCCL_IGNORE_CPU_AFFINITY`

**`NCCL_IB_DISABLE`: specified.** "prevents the IB/RoCE transport from being
used by NCCL. Instead, NCCL will fall back to using IP sockets." The
documentation defines only the value `1` and states no default. The source does:
default `0` (`src/transport/net_ib/init.cc:27`), single use site
`if (ncclParamIbDisable()) return ncclInternalError;` (`init.cc:291`). **`=0` is
a strict no-op** — see 0.13.

**`NCCL_IGNORE_CPU_AFFINITY`: specified.**

> "By default, NCCL uses the intersection of the inherited CPU affinity and the
> CPU affinity associated with the GPU. … Setting this variable to 1 makes NCCL
> ignore the inherited affinity and use the GPU affinity only. … The default is
> 0."

(`src/graph/topo.cc:2212`, `topo.cc:2231-2237`.) The reason to set 1 is a
launcher that has pinned the process to CPUs off the GPU's NUMA node; NCCL's
helper threads would otherwise land far from the GPU and its NIC. It cannot
escape a cgroup.

**Why upstream sets them: unspecified.** `launch-cluster.sh:10` sets
`NCCL_IGNORE_CPU_AFFINITY=1` in the base `DOCKER_ARGS` with no comment, and
`launch-cluster.sh:967` sets `NCCL_IB_DISABLE=0` in `get_env_flags` with no
comment. Neither `README.md` nor `docs/NETWORKING.md` explains either. We
reproduce both (`engines/defaults/vllm.yaml:51`, `engines/vllm.py:111`) without
a rationale of our own, which is the honest position given the sources.

Worth noting `get_env_flags` (`launch-cluster.sh:957-974`) emits every pinning
variable unconditionally, so at solo — where `detect_interfaces` is skipped
(`launch-cluster.sh:508-521`) and `ETH_IF`/`IB_IF` are empty — upstream sets
`NCCL_SOCKET_IFNAME=` and `NCCL_IB_HCA=` to the empty string. We omit them
instead (`engines/base.py:464-466`), a deliberate and better divergence: per §2
an empty `NCCL_SOCKET_IFNAME` falls through to auto-selection anyway, but
omitting is unambiguous.

### 4. The default collective timeout

**Specified, and mis-attributed by us.** Full treatment in 0.4 and 0.5. In
summary:

| Clock | Default | Owner | Where |
|---|---|---|---|
| Collective / work timeout | **600 s (10 min)** | PyTorch `ProcessGroupNCCL` | `ProcessGroupNCCL.hpp:158-159`; `init_process_group(timeout=…)` |
| Watchdog heartbeat | 480 s (8 min) | PyTorch | `TORCH_NCCL_HEARTBEAT_TIMEOUT_SEC` |
| `TCPStore` init/connect | 300 s | PyTorch | `TCPStore` docstring |
| torchrun rendezvous join | 600 s | PyTorch Elastic | `RendezvousTimeout._DEFAULT_TIMEOUTS["join"]` |
| IB verbs attempt | ≈4.3 s × `IB_RETRY_CNT` | NCCL | `NCCL_IB_TIMEOUT`, default 20 |
| Socket retry budget | ≈60 s | NCCL | `NCCL_SOCKET_RETRY_CNT` 34 × 100 ms |
| Collective timeout | **none** | NCCL | no such variable exists |

The correction to make wherever we state it: it is PyTorch's default, it is
configurable only through `timeout=` (there is no environment variable for it),
and `NCCL_TIMEOUT` is not a variable NCCL reads.

### 5. The different-subnet rule for the two devices of one port

**Specified, by exactly one NVIDIA source.**
[`multi-sparks-through-switch/README.md`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/multi-sparks-through-switch/README.md):

> "`enp1s0f1np1` and `enP2p1s0f1np1` are assigned to **different subnets**
> (`192.168.100.x/24` and `192.168.101.x/24` respectively). This is required —
> assigning two distinct network interfaces to the same subnet causes networking
> and software conflicts (e.g., routing ambiguity and NCCL communication
> failures)."

Notably **not** stated in the
[DGX Spark clustering guide](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html),
which explains the twin-interface topology in full and warns that confusion here
"can lead to mistakes in step three and prevent the cluster from working
correctly" without saying what the mistake is; nor in `connect-two-sparks` or
`connect-three-sparks`, which use different subnets in every example without
stating the rule.

**Contested by NVIDIA's own automation.**
`detect_and_configure_cluster_networking.py` puts both twins of a link on the
same `/24` in two-node and switch modes (`ip_for_2node_link`: "network =
192.168.link_index.0/24, hosts .1 .. .4 used for the two nodes (2 endpoints
each)"), and reserves distinct subnets per twin only for the three-node ring
(`ip_for_3node_ring_link`). It also always exports
`NCCL_IB_SUBNET_AWARE_ROUTING=1`, which may be why same-subnet is tolerable
there — NVIDIA does not say.

**What breaks: unspecified beyond one clause.** The only causal statement from
any source is "routing ambiguity and NCCL communication failures". Upstream adds
"it will confuse autodiscovery and mess up routing" (`docs/NETWORKING.md:133`);
[sparkrun.dev](https://sparkrun.dev/getting-started/networking/) echoes it. No
source I checked — NVIDIA docs, the playbooks, the DGX Spark forum category —
names ARP flux, asymmetric routing, RoCE GID selection or wrong-port egress as
the mechanism. Adjacent evidence that L3 configuration bites in practice:
[NVIDIA/daqiri#173](https://github.com/NVIDIA/daqiri/issues/173), "ping across
bench IPs fails until `ip route` and `ip neigh` on both hosts" — about missing
routes, not same-subnet twins.

**We follow the stricter rule** (upstream's), and do not check it (0.11).

### 6. Three- and four-node cabling and bandwidth

**Port 0 → port 1 for three nodes: specified.**
[`connect-three-sparks/README.md`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-three-sparks/README.md):

> "Here, Port0 is the CX7 port next to the Ethernet port and Port1 is the CX7
> port further away from it. 1. Node1 (Port0) to Node2 (Port1) 2. Node2 (Port0)
> to Node3 (Port1) 3. Node3 (Port0) to Node1 (Port1)"

which differs from the two-node rule — "Make sure to use the same physical port
on each device to prevent issues with NCCL tests"
([`connect-two-sparks`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/README.md)).
Upstream states the same, independently: `docs/NETWORKING.md:50`, "port 0 on one
Spark should connect to port 1 on another Spark (unlike non-mesh
configuration)", with the diagram at `:53-82`. NVIDIA also requires all four
interfaces addressed in the ring — "In a three node ring topology all four
interfaces on each node must be assigned an IP address to form a symmetric
cluster." Upstream's mesh netplan does exactly that
(`docs/NETWORKING.md:162-253`), and its mesh `IB_IF` names all four RoCE devices
(`autodiscover.sh:167`).

**Four nodes direct: specified as unsupported.** See 0.3.

**Per-pair bandwidth: contested.**

* NVIDIA prose: "Each CX7 port provides full 200GbE bandwidth."
  (`connect-three-sparks`).
* NVIDIA code: `spark_cluster_setup.py` sets `MIN_NCCL_TEST_BW = 21.875 # 175
  Gbps` for the general case and `MIN_NCCL_TEST_BW_RING = 10 # 80 Gbps` for
  `ring_topology`. That is roughly half, from NVIDIA's own thresholds — though
  it is an all-gather busbw floor, not a per-link statement.
* Upstream: "If you connect 3 Sparks by daisy-chaining them, you will only be
  able to sustain 100G between each pair of Sparks." (`docs/NETWORKING.md:43`).
* Contradicted on the forum by `adg1`: "You can cluster up to 3 Sparks,
  following a ring topology and without sacrificing interconnect bandwidth."
  ([6x Spark setup](https://forums.developer.nvidia.com/t/6x-spark-setup/354399),
  post 62).
* No measured per-pair number for a three-node ring exists in any source I
  found.

We follow the halving claim (`engines/base.py:436-437`,
`web/src/lib/experimental.ts:29`), which is the conservative reading and the one
NVIDIA's own threshold constants support.

**Two nodes: specified.** One QSFP cable, same physical port both ends, 200 Gb/s
per port. "Full bandwidth can be achieved with just one QSFP cable"; the
[Cluster Assistant doc](https://docs.nvidia.com/sync/latest/cluster-assistant.html)
adds "Connecting two devices with two cables will not improve performance",
corroborating `docs/NETWORKING.md:42` and NVIDIA employee `isdias` ("Using two
cables shouldn't provide any speed improvements", thread 350417 post 65).
Approved cables are listed in the clustering guide. Measured: ~185–195 Gb/s
community; upstream's own `ib_write_bw` on a *single* device reports 111.71
Gb/s average (`docs/NETWORKING.md:312`) — one twin, i.e. one PCIe x4 half.

**The 6–8 node forum thread** linked at `docs/NETWORKING.md:5` and `:87` is
[forums.developer.nvidia.com/t/6x-spark-setup/354399](https://forums.developer.nvidia.com/t/6x-spark-setup/354399).
It is **not a mesh**. `ericlewis777` (post 34): "8x DGX Spark (6x founder, 2x hp
zgx nano) 2x CRS812 DDQ 1x DDQ+DA0001 … The DDQ 400gbps cable links the
switches, leaving 8 usable 200gbps connects. … It pretty much just works."
Findings worth carrying:

* Six nodes is a dead zone for tensor parallel — "almost nothing useful can be
  done with 6x, you need either 4 or 8" (post 77), matching
  `docs/NETWORKING.md:6`.
* Throughput scales: GPT-OSS-120B at concurrency 100, 375 / 624 / 1300 tok/s at
  1 / 2 / 4 nodes (post 21).
* The fabric is not the bottleneck at this scale — "Outside of specifically
  doing testing to measure max bandwidth, I've never seen >80Gbps" (post 43).
* MTU matters: default `active_mtu` 1024 at Ethernet MTU 1500; raise the host
  MTU (posts 65–71). Upstream sets `mtu: 9000` throughout its netplan.
* **No NCCL environment variables appear anywhere in the thread**, and no NVIDIA
  staff post appears in it. There is **no statement from anyone about whether
  more than four nodes is supported** — see the unspecified list.

---

## Engine rendezvous

### 7. vLLM: `--headless` and how the store is chosen

**Flags: specified.** `--master-addr`, `--master-port`, `--nnodes`/`-n`,
`--node-rank`/`-r` are stock `vllm serve` arguments —
[`vllm/engine/arg_utils.py:1040-1043`](https://github.com/vllm-project/vllm/blob/3284af6bf1be8429c332bd5fafba579c2d7557da/vllm/engine/arg_utils.py#L1040-L1043)
— with defaults `master_addr = "127.0.0.1"`, `master_port = 29501`,
`node_rank = 0`, `nnodes = 1`
([`vllm/config/parallel.py:277-288`](https://github.com/vllm-project/vllm/blob/3284af6bf1be8429c332bd5fafba579c2d7557da/vllm/config/parallel.py#L277-L288)).
Our default rendezvous port is the same 29501
(`engines/defaults/vllm.yaml:47`, `engines/vllm.py:209`).

**"Since 0.11.1": specified by tags, unspecified by the changelog.** Added by PR
#23691, commit `b316ac6589` (2025-11-16). `v0.11.0` does not contain the flags;
`v0.11.1` has them at `arg_utils.py:757-760`; `git tag --contains b316ac6589`
starts at `v0.11.1`. The
[v0.11.1 release notes](https://github.com/vllm-project/vllm/releases/tag/v0.11.1)
do not mention them. Our `min_framework_version = (0, 11, 1)`
(`engines/vllm.py:99`) is therefore exactly right, verifiable only from source.

**`--headless`: specified, and dual-purpose.** Registered at
`vllm/entrypoints/launchers/cli_args.py:386-392`. Older than the rendezvous
flags — present in `v0.9.0`, where it meant "run DP engines, no API server".
Since #23691 it branches on rank: `vllm/entrypoints/cli/serve.py:211-237`, when
`node_rank_within_dp > 0` it starts a bare `MultiprocExecutor` joined via
`master_addr:master_port` and returns; otherwise the legacy DP path. In both
cases the API server is suppressed. We render it for every rank above zero
(`engines/vllm.py:220-221`), matching upstream `launch-cluster.sh:1214` and
vLLM's own documented example.

vLLM's [parallelism guide](https://docs.vllm.ai/en/stable/serving/parallelism_scaling/)
gives the non-Ray form we render:

> `vllm serve … --nnodes 2 --node-rank 0 --master-addr <HEAD_NODE_IP>` … "On the
> other worker node, run: … `--node-rank 1 --master-addr <HEAD_NODE_IP>
> --headless`"

**The store claim: refuted.** See 0.2. Truth table for current `main`:

| `nnodes` | `data_parallel_size` | Store | Driven by |
|---|---|---|---|
| 1 | 1 | FileStore | tempfile, node-local |
| 1 | >1 | **TCPStore** | `data_parallel_master_ip` + `get_next_dp_init_port()` |
| >1 | any | TCPStore | `master_addr` + `master_port` |
| any | any, `external_launcher` | unchanged (`env://`) | override skipped |

Store type is chosen purely by URI scheme (`parallel_state.py:1819-1823`).

**A divergence from upstream worth stating plainly.** Upstream appends the
rendezvous flags only on the multi-node path (`launch-cluster.sh:1214`, `:1231`,
inside `exec_no_ray_cluster`); at solo the command is left alone
(`run-recipe.py:519-520` strips only `--distributed-executor-backend`). We
append them at **every** size (`engines/vllm.py:165`, `:206-222`). That remains
safe for the reasons in 0.2, but it is a divergence justified by a claim that
needs the rewrite 0.2 proposes.

### 8. SGLang: `--dist-init-addr` at one node and above

**Semantics: specified.**
[`server_args.py:921-928`](https://github.com/sgl-project/sglang/blob/fbf8f1dbf69f4f91ac6654383227e82a8a932217/python/sglang/srt/server_args.py#L921-L928):
"The host address for initializing distributed backend (e.g.
`192.168.0.2:25000`)", alias `--nccl-init-addr`, default `None`. `--nnodes`
defaults to 1 and `--node-rank` to 0.

**Read at `--nnodes 1`: specified.** There is no node-count guard. In
`v0.5.10.post1` — the tag our image pins (`engines/defaults/sglang.yaml:8`,
`:14`) —
[`model_runner.py:917-930`](https://github.com/sgl-project/sglang/blob/v0.5.10.post1/python/sglang/srt/model_executor/model_runner.py#L917-L930):

```python
elif self.server_args.dist_init_addr:
    na = NetworkAddress.parse(self.server_args.dist_init_addr)
    dist_init_method = na.to_tcp()
else:
    dist_init_method = NetworkAddress(
        self.server_args.host or "127.0.0.1", self.dist_port
    ).to_tcp()
```

Confirmed empirically by
[sglang#15385](https://github.com/sgl-project/sglang/issues/15385), where
`--dist-init-addr 10.86.158.116:33333 --nnodes 1` fails with `EADDRINUSE` on
port 33333 — the address was demonstrably bound at one node. So our
`sglang.py:8-10` claim that SGLang honours the address at one node is **correct
and now cited**.

**`host:port` is mandatory: specified.** `NetworkAddress.parse` raises "Missing
port in address (expected host:port)"; bare IPv6 must be bracketed. We always
render a port (`engines/sglang.py:111`).

**Loopback at size one: contested.** See 0.12. Working, but not what SGLang
documents, and it flips `dist_init_addr is not None` multi-node branches. Note
the fallback when unset is version-dependent: `v0.5.2` hardcoded `127.0.0.1`;
`v0.5.10+` uses `server_args.host or "127.0.0.1"`, so with our `--host 0.0.0.0`
(`engines/sglang.py:37`) omitting the flag would yield `tcp://0.0.0.0:{port}` —
a wildcard. That is a real argument for setting loopback explicitly rather than
omitting, and the strongest defence of our current behaviour; it is just not the
one the docstring gives.

**Cross-check against `mark-ramsey-ri/sglang-dgx-spark`** — the GB10 hardware
evidence `docs/native-runtime-plan.md:117-155` cites. README: "Verified
end-to-end on 1 and 2 Sparks; the n>2 code paths are reviewed but not yet
exercised on real hardware"; hardware "1x NVIDIA GB10 … ~120GB unified memory".

| | That repo | Us |
|---|---|---|
| `--dist-init-addr` at 1 node | `${HEAD_IP}:50000` — the **RoCE IP**, auto-detected from `ibdev2netdev … grep "^enp1"`; no single-node branch drops it | `127.0.0.1:50000` (`sglang.py:102`) |
| `--dist-init-addr` at N nodes | `${HEAD_IP}:50000` | `topology.head.address():50000` |
| `NCCL_IB_HCA` | auto-detected `ibdev2netdev \| grep "(Up)" \| awk '{print $1}'`, comment "typically `rocep1s0f1,roceP2p1s0f1`" | same form, but see 0.1 — we cannot derive it |
| `NCCL_SOCKET_IFNAME` / `GLOO_SOCKET_IFNAME` | auto-detected, prefers `^enp1` | registry `ethernet_interface`, no preference (0.10) |
| `NCCL_IB_DISABLE`, `NCCL_NET_GDR_LEVEL`, `NCCL_TIMEOUT` | `0`, `5`, `1200000` | identical (`sglang.yaml:31-33`) — see 0.5, 0.6 |
| `--enable-dp-attention` | default `EXTRA_ARGS`, **all sizes** | multi-node only (`sglang.yaml:42-46`, `sglang.py:115-116`) |
| Start order | workers first, then `sleep 5`, then head | workers first, head last (`native_runtime.py:280-286`) |

Our restriction of `--enable-dp-attention` to multi-node is the better choice
and matches `docs/native-runtime-plan.md:151` ("Solo does not need either").
Pointing `--dist-init-addr` at loopback rather than the RoCE IP at size one is a
deliberate, defensible divergence — but it means the size-one configuration we
ship is **not** the one that was verified on hardware.

### 9. Start ordering: workers first, head last

**Unspecified. It is folklore, and where any order is prescribed it is the
opposite.**

Sources checked, and what each fails to say:

* **SGLang docs** — the
  [multi-node page](https://docs.sglang.io/docs/references/multi_node_deployment/multi_node)
  shows the rank-0 block then the rank-1 block with no sentence about sequence.
  Its SLURM example is explicitly *simultaneous*: one `srun --ntasks=2` fans out
  both ranks at once. A grep of the whole `docs/` and `examples/` trees for
  ordering language returns nothing.
* **vLLM docs** — prescribe **head first**, the opposite.
  `examples/ray_serving/multi-node-serving.sh`: "This script is first executed
  on the head node, and then on each worker node with the IP address of the head
  node." The Ray path genuinely requires it; the non-Ray path we render presents
  head-first but states no requirement.
* **PyTorch** — says order does not matter. Rank 0 is the `TCPStore` server;
  everyone else is a client, and `TCPStore.cpp:306-352` retries with exponential
  backoff and jitter until `timeout` elapses. Symmetrically, rank 0 blocks in
  `waitForWorkers()` until `world_size` clients arrive (`wait_for_workers`
  defaults to True, `timeout` to 300 s). torchrun's own documentation says to
  "Start `torchrun` with the **same arguments on all the nodes**".
* **NVIDIA DGX Spark playbooks** — cover netplan, SSH and `nccl-tests` via
  `mpirun`; the SGLang playbook is single-node. None prescribes a server start
  order.
* **Upstream** — *does* do it, and is where we got it. `launch-cluster.sh:1205`
  `# Launch workers first (always background)`, `:1224` `# Launch head (rank 0)
  last`. No rationale is given anywhere in that repo.
* **`mark-ramsey-ri/sglang-dgx-spark`** — `start_cluster.sh:343`
  `# Step 4: Start workers via SSH (before head, so they're ready to connect)`,
  followed by an unexplained `sleep 5`. That parenthetical is the only stated
  reason in any source, and it cites nothing.

**Consequences for us.** Two of our statements need correcting:

* `native_runtime.py:20-23` and `:280-282` call it "upstream's proven order".
  Upstream *uses* it; nobody has shown it is proven, and PyTorch's retry
  behaviour suggests either order converges. Call it "upstream's order".
* `native_runtime.py:1387-1391` and `docs/cluster-agent-plan.md:308` justify
  head-first *teardown* by "so no worker sits in a collective timeout". That
  reasoning is sound and independent of start order — killing the store server
  collapses the clients — but the timeout is PyTorch's (§4).

The item at `web/src/lib/experimental.ts:27` — "Whether starting workers before
rank zero really avoids the ten-minute collective timeout" — is well posed. It
should say *PyTorch's* ten-minute collective timeout, and it can now add that no
source prescribes the ordering at all.

---

## The unspecified residue

Eight items. Nobody documents these; the sources checked are named.

1. **Whether naming both RoCE twins aggregates bandwidth on GB10.** NCCL's
   documentation calls `NCCL_IB_HCA` a filter and never addresses it; NCCL
   maintainer statements say aggregation needs NVLink between GPUs, which does
   not describe one GPU with two NIC halves. The affirmative comes only from
   NVIDIA-employee forum prose and NVIDIA's own script, never from a document.
   *Checked:* NCCL env reference, NCCL troubleshooting, NCCL source
   (`net_ib/init.cc`, `graph/search.cc`), nccl#412/#981/#1084, DGX Spark
   clustering guide, all five relevant playbooks.
2. **What actually breaks when the two devices of one port share a subnet.** One
   parenthetical — "routing ambiguity and NCCL communication failures" — and
   nothing else. No mechanism, no reproduction, no log signature.
   *Checked:* DGX Spark clustering guide, `connect-two-sparks`,
   `connect-three-sparks`, `multi-sparks-through-switch`, Cluster Assistant doc,
   the DGX Spark forum category.
3. **Per-pair bandwidth on a three-node ring.** NVIDIA prose says full 200GbE
   per port; NVIDIA's script sets a ring floor less than half the general one;
   upstream says 100G per pair; a forum participant says no loss. No measurement
   from anyone. *Checked:* `connect-three-sparks`, `spark_cluster_setup.py`,
   upstream `docs/NETWORKING.md`, forum 354399.
4. **Whether more than four nodes is supported.** No statement from NVIDIA
   either way. The only ceiling anyone states is what the *Cluster Assistant*
   supports (three direct, four switched), which is a tool limit, not a hardware
   or NCCL one. The 6–8 node thread contains no NVIDIA post.
   *Checked:* clustering guide, Cluster Assistant doc, playbooks, forum 354399
   (113 posts), forum 368726. Our `MAX_CLUSTER_NODES = 4` is a policy choice,
   not a documented limit — which is fine, but the docstring should say so.
5. **Whether workers-first ordering helps at all.** §9. No source prescribes any
   order for either engine; PyTorch's store retries make both orders converge.
6. **Whether `GLOO_SOCKET_IFNAME=lo` is needed at one node.** The rationale in
   `engines/base.py:459-462` — Gloo resolves the hostname and fails on a host
   whose hostname does not resolve — is plausible, and I found no PyTorch
   document stating it. *Checked:* PyTorch distributed docs, Gloo docs,
   `torch/distributed/rendezvous.py`.
7. **Why upstream sets `NCCL_IGNORE_CPU_AFFINITY=1` on this hardware.** The
   variable's meaning is documented; the reason to set it on a GB10 with one GPU
   and a unified memory pool is not, in any source. §3.
8. **`--mem-fraction-static` / `gpu_memory_utilization` for two-node SGLang.**
   NVIDIA's SGLang playbook is single-node only (`--mem-fraction-static 0.75`);
   its vLLM playbook gives 0.4 single-node NVFP4, 0.8 two-node TP2, 0.9 for
   405B, and passes none at all for TP4. `mark-ramsey-ri` uses 0.90 with 0.85 as
   a fallback. There is no vendor two-node SGLang number.
   `docs/cluster-agent-plan.md` section 7 summarises the vLLM figures correctly.

---

## Sources

**Upstream** — `eugr/spark-vllm-docker` @ `358bf26`, read at
`ssh alex@192.168.29.60:~/projects/spark-vllm-docker`: `docs/NETWORKING.md`,
`autodiscover.sh`, `launch-cluster.sh`, `run-recipe.py`, `README.md`.

**NVIDIA**

- [DGX Spark clustering / ConnectX-7](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
- [Sync Cluster Assistant](https://docs.nvidia.com/sync/latest/cluster-assistant.html)
- [`connect-two-sparks`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-two-sparks/README.md) ·
  [`connect-three-sparks`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/connect-three-sparks/README.md) ·
  [`multi-sparks-through-switch`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/multi-sparks-through-switch/README.md) ·
  [`nvidia/nccl`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/nccl/README.md) ·
  [`nvidia/vllm`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/vllm/README.md) ·
  [`nvidia/sglang`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/sglang/README.md)
- [`spark_cluster_setup.py`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/multi-sparks-through-switch/assets/spark_cluster_setup/spark_cluster_setup.py) ·
  [`detect_and_configure_cluster_networking.py`](https://github.com/NVIDIA/dgx-spark-playbooks/blob/main/nvidia/multi-sparks-through-switch/assets/spark_cluster_setup/node_scripts/detect_and_configure_cluster_networking.py)

**NCCL**

- [Environment variables (2.31.2)](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html) ·
  [Troubleshooting](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting.html)
- [Source @ `fd16832`](https://github.com/NVIDIA/nccl/tree/fd168324a3dc0c9080fd4881b6c7f4bb252a95a2):
  `src/misc/socket.cc`, `src/misc/utils.cc`, `src/os/linux.cc`,
  `src/bootstrap.cc`, `src/transport/net_socket.cc`,
  `src/transport/net_ib/init.cc`, `src/graph/search.cc`, `src/graph/topo.cc`,
  `src/plugin/net.cc`
- [nccl#412](https://github.com/NVIDIA/nccl/issues/412) ·
  [#981](https://github.com/NVIDIA/nccl/issues/981) ·
  [#1084](https://github.com/NVIDIA/nccl/issues/1084)

**PyTorch** —
[`ProcessGroupNCCL.hpp`](https://github.com/pytorch/pytorch/blob/main/torch/csrc/distributed/c10d/ProcessGroupNCCL.hpp),
`ProcessGroupNCCL.cpp`,
[`TCPStore.cpp`](https://github.com/pytorch/pytorch/blob/main/torch/csrc/distributed/c10d/TCPStore.cpp),
`torch/distributed/constants.py`, `torch/distributed/distributed_c10d.py`,
`torch/distributed/elastic/rendezvous/dynamic_rendezvous.py`,
`torch/distributed/launcher/api.py`,
[distributed docs](https://docs.pytorch.org/docs/stable/distributed.html),
[torchrun docs](https://docs.pytorch.org/docs/stable/elastic/run.html)

**vLLM** — source @
[`3284af6`](https://github.com/vllm-project/vllm/tree/3284af6bf1be8429c332bd5fafba579c2d7557da)
(`engine/arg_utils.py`, `config/parallel.py`,
`v1/executor/multiproc_executor.py`, `distributed/parallel_state.py`,
`utils/network_utils.py`, `entrypoints/cli/serve.py`), PR #23691, PR #50999,
[parallelism guide](https://docs.vllm.ai/en/stable/serving/parallelism_scaling/),
[v0.11.1 release notes](https://github.com/vllm-project/vllm/releases/tag/v0.11.1)

**SGLang** — source `v0.5.10.post1` and `main` (`srt/server_args.py`,
`srt/model_executor/model_runner.py`, `srt/distributed/bootstrap.py`,
`srt/utils/network.py`, `srt/disaggregation/common/conn.py`),
[server arguments](https://docs.sglang.io/docs/advanced_features/server_arguments),
[multi-node deployment](https://docs.sglang.io/docs/references/multi_node_deployment/multi_node),
[PR #23158](https://github.com/sgl-project/sglang/pull/23158),
[issue #22877](https://github.com/sgl-project/sglang/issues/22877),
[issue #15385](https://github.com/sgl-project/sglang/issues/15385)

**Community** — `mark-ramsey-ri/sglang-dgx-spark` (`start_cluster.sh`,
`config.env`, `README.md`);
[6x Spark setup](https://forums.developer.nvidia.com/t/6x-spark-setup/354399) ·
[ConnectX-7 NIC in DGX Spark](https://forums.developer.nvidia.com/t/connectx-7-nic-in-dgx-spark/350417) ·
[4-node without a switch](https://forums.developer.nvidia.com/t/4-node-dgx-spark-cluster-without-a-switch/368726) ·
[sparkrun.dev networking](https://sparkrun.dev/getting-started/networking/) ·
[NVIDIA/daqiri#173](https://github.com/NVIDIA/daqiri/issues/173) ·
[alexellis/glm-5.3-flash-4x-dgx-spark-switchless](https://github.com/alexellis/glm-5.3-flash-4x-dgx-spark-switchless)
