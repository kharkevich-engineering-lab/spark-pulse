# Monitoring

Two different things are called monitoring here, and they come from different places.

## What the machines are doing

`GET /api/memory` and the `/sse/metrics` stream ask **every registered node** for its own stats through that node's agent — including the machine the control plane runs on.

```mermaid
sequenceDiagram
  participant P as Fleet · Monitoring
  participant CP as Control plane
  participant N1 as Agent · spark-01
  participant N2 as Agent · spark-02
  P->>CP: GET /api/memory
  par every node, in parallel
    CP->>N1: GetNodeStats
    N1-->>CP: GPUs, memory, disks, GPU processes
  and
    CP->>N2: GetNodeStats
    N2--xCP: no answer
  end
  CP->>N1: ListManagedContainers
  N1-->>CP: which containers are ours, and for which deployment
  CP-->>P: nodes: [spark-01 …, spark-02 unreachable + why]
```

Two halves decide whether a GPU process is *ours*: the node says which container the process is in — it can read that, the control plane cannot — and the control plane's managed-container list says which containers it started and for which deployment. So a held GPU names the deployment holding it, and anything else is *untracked*.

**Absent measurements stay absent.** A GB10 reports `[N/A]` for GPU memory because host and device share one pool. Every measurement in the protocol is optional, and the page says *unified memory — usage not reported by nvidia-smi* rather than drawing an empty bar for a full machine.

**A node that could not be asked keeps its section** and says why. A missing section and an idle machine look identical on a page.

### The shape of free memory

`MemAvailable` says how much memory is free, but not what shape it's in. On unified memory the GPU maps weights with large pages, and a large page needs a physically contiguous block under it. The agent reads `/proc/buddyinfo` (free blocks per order, summed over zones) along with the page size. `tools/memory_shape.py` turns that into **Contiguous free** on the node page: how much of the free memory is in blocks of 2 MiB or more, and the largest block. A card with memory of its own shows nothing, because the host's pool is not where its weights go.

The buddy allocator's free memory excludes the page cache, so it is usually far below `MemAvailable`. The pre-flight's **Memory shape** line reports that gap as cache the kernel can drop. It warns, and never blocks, when less than half of at least 1 GiB of free memory is in blocks of 2 MiB or more. The remedy it gives runs only with no engine on the node: `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory'`. The 50% threshold is a first guess, to be moved by measurement.

For reference, a DGX Spark runs 4 KiB pages with 14 orders, so its largest possible block is 32 MiB. On 2026-11-23, 21 days up with a run loaded, one had 1.5 GiB free, 52% of it in 2 MiB+ blocks, and 22 GiB of cache.

### Compacting memory before a run

Settings → **Memory** has two switches and an **Enable all** button, saved with the rest of the form. Both are off by default.

**Compact before launch** (`memory.compact_before_launch`). After the images and the model are ready and before the first rank starts, the control plane asks each node of the run, in parallel, to run what the remedy above runs: `sync`, then `3` into `/proc/sys/vm/drop_caches`, then `1` into `/proc/sys/vm/compact_memory`. It is the agent's `CompactMemory` operation, and the agent is rootless, so each write goes through `sudo -n /usr/bin/tee <path>` with the value on stdin. The agent installer's sudoers drop-in, `/etc/sudoers.d/spark-pulse-agent-memory`, grants exactly those two argument vectors, with no wildcard:

```
<user> ALL=(root) NOPASSWD: /usr/bin/tee /proc/sys/vm/drop_caches, /usr/bin/tee /proc/sys/vm/compact_memory
```

Because the rule names the whole command line, it cannot be used to write any other file. It is installed under the same opt-in as the network rule (the browser onboarding sets it); a node enrolled before this existed needs its agent reinstalled once.

- **A node another run is on is skipped.** Dropping caches and compacting stall everything on the node, so a serving engine is left alone. The test is the one the port choice and memory budget use (`placement._occupied_nodes`), less the run being started.
- **It never fails the deploy.** A missing sudoers rule, an agent too old for the operation, a timeout (60 s per write) or a node that cannot be asked each leave an event saying so, and the run goes on.
- Each node leaves one event on the run, for example `spark-02: 25.1 GiB → 80.3 GiB free in blocks of 2 MiB or more`. The deploy preview says memory will be compacted when the switch is on.

**Lazy NCCL communicators** (`memory.lazy_nccl_communicators`, experimental). Sets `TORCH_DIST_USE_NCCL2=0` on every rank of a multi-node run, for any engine, so that torch builds NCCL communicators when first used rather than all at once. It applies to torch 2.16 and later; the claim that it returns memory on a head rank comes from one operator's three-node run and is not yet measured on our images. A recipe that sets the variable itself wins. It is part of how a run is deployed, not of what it is, so it does not change the run's `intent_hash`.

### Ending a process

`DELETE /api/memory/processes/{pid}?node=…` works on any node:

- If the process is inside a container this control plane started, the **container** is stopped. Killing the process inside would leave the container holding its ports, and whatever supervises it would start the process again.
- Otherwise the process is signalled through that node's agent.

The same pid on two Sparks is two different processes, which is why the node is part of the address.

## What an engine is doing

Separately, a sampler scrapes each running deployment's own `/metrics` — Prometheus text — every five seconds into a ring of 720 readings: one hour, in memory, lost on restart. `GET /api/deployments/{id}/metrics` returns that window with an `available` / `reason` / `detail` triple, so a deployment publishing nothing (SGLang without `--enable-metrics`, an unreachable endpoint, a body nothing recognises) says which of those it is instead of showing an empty chart.

Token throughput is differenced from counters. A counter that goes backwards is an engine restart, so that interval's rate is `null` — never a negative spike.

There are no percentiles: both engines publish histograms only, and inventing a p99 from a histogram bucket would be making one up.

There is deliberately no persistence. Retention is Prometheus's job; this window exists so you can see the last hour without deploying one.
