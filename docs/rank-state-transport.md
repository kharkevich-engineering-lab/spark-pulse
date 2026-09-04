# Rank state: what transport, and does the fan-out change the answer

Status: research, 2026-09-04. Re-examines `cluster-agent-plan.md` §2.1a against a
workload that section did not measure.

Measurements were taken this session from this control plane over the LAN to the
DGX Spark at `192.168.29.60` (`gx10-ced2`, GB10, 20 cores, DGX OS on Ubuntu
24.04, OpenSSH 9.6p1, Docker 29.2.1), using `spark_pulse.tools.ssh.OpenSSHClient`
exactly as shipped. Nothing was deployed, built or written on that machine; every
remote command was a read.

---

## The recommendation

**Do not build the agent for this. Keep SSH, and spend a fraction of the agent's
cost on three specific fixes.** The fan-out case does not make the agent's case,
because fan-out over SSH is not expensive: four concurrent `docker inspect`, one
per node, complete in **41 ms** wall and cost each node **10.6 process creations
and 16.8 ms of CPU** — 0.17% of one core of twenty, sustained, at a ten-second
poll. The number in the code comment that motivated fetch-on-expand — "four SSH
round trips per four-node deployment on every ten-second poll"
(`web/src/pages/InferencePage.tsx:93-105`) — is 130 ms done serially as we do it
today, and 60 ms done concurrently. Reaching for SSH per inspect is not the wrong
primitive on cost.

It is the wrong *shape* in exactly one place, and that place is not speed either.
It is the node that does not answer: **10.0 s per probe, on every poll,
indefinitely** — measured nine consecutive times over ninety seconds — and
`native_runtime.status()` runs its per-rank inspects in a serial list
comprehension (`spark_pulse/tools/native_runtime.py:2040`), so one silent rank
turns `GET /api/deployments/{id}` into a ten-second request. That is the cost SSH
has and an agent does not, and §2.1a never measured it because it only ever timed
the happy path.

So §2.1a's **conclusion stands and its reasoning should be revised**. The
sentence that does not survive contact with this workload is "All three remaining
justifications only bite at more than one node." Two of the three bite now, at
one remote node, on a path an operator can already reach — and one of them is a
live wrong answer in shipped code (§2.4 below). What §2.1a got right, and what
this measurement reinforces, is that the agent is not the cheapest way to fix
any of them.

The three fixes, in order, none of which needs a second machine:

1. **Stop reporting a dead Docker daemon as a missing container.**
   `RemoteNodeService.get_container_status` treats any non-zero `docker inspect`
   exit as "not found" (`spark_pulse/tools/node_service.py:662-669`). Measured on
   the Spark: `docker inspect no-such-container` exits 1, and `docker inspect`
   against an unreachable daemon *also* exits 1. The control node's own rank does
   not have this bug — `DockerService.get_container_status` separates `NotFound`
   from `APIError` (`spark_pulse/tools/docker.py:777-786`). So the third state
   exists on the control node and is destroyed on every peer.
2. **Make `status()` concurrent, and give the liveness probe its own short
   timeout.** Measured: 130 ms → 60 ms for four healthy ranks; and a silent rank
   stops costing a full interval. §2.1a already proposed the short timeout as a
   cheaper mitigation and it has not been built.
3. **One long-lived `docker events` tail per node, over the SSH connection we
   already hold**, feeding the existing `EventBroadcaster`
   (`spark_pulse/tools/events.py:117`), with the inspect demoted to a slow
   reconcile. Measured through the shipped client: **29 ms to first bytes, 196
   historical events replayed, still streaming at 15 s**, delivery latency of
   2.7 ms median over a held channel, and an idle held channel costing less than
   the Spark's own background noise. This is push. It buys the "report a state
   change without being asked" property outright, and it needs no new daemon, no
   CA, no enrollment and no wire contract of ours.

That leaves exactly one of §2.1a's three justifications genuinely unavailable
over SSH — a **typed** transport error, so that no layer above `ssh.py` has to
read English. Section 8 says what the smallest agent that buys it looks like, and
it is much smaller than §4 phase D.

---

## 1. The measurement §2.1a should have taken

### 1.1 The baseline reproduces

Same client, same flag, same LAN, twenty-five repetitions each, medians:

| operation | no multiplexing | multiplexed, warm |
|---|---|---|
| round-trip floor (`true`) | 511 ms | 17 ms |
| `docker inspect --format '{{json .State}}'` | 531 ms | 30 ms |
| multiplexed with the master killed before each call | — | 515 ms |

§2.1a recorded 517/17 ms for the floor and its container list at 527/36 ms. This
agrees to within a few milliseconds, so everything below is measured against the
same machine in the same state that section was.

### 1.2 ControlMaster does not serialise. It parallelises, up to exactly ten.

This was the crux, and the answer is clean. Five repetitions at each width,
median wall time for the whole burst, zero failures anywhere:

| concurrent inspects | over **one** master | over **N separate** masters |
|---|---|---|
| 1 | 28.7 ms | 40.5 ms |
| 2 | 31.8 ms | 33.1 ms |
| 3 | 30.9 ms | 34.3 ms |
| **4** | **36.6 ms** | **41.4 ms** |
| 6 | 36.9 ms | 42.1 ms |
| 8 | 44.1 ms | 44.9 ms |
| 10 | 59.0 ms | — |
| 12 | **575.5 ms** | 50.3 ms |
| 16 | **585.5 ms** | 58.9 ms |

Four concurrent inspects cost 37 ms where one costs 29 ms. They genuinely
overlap: the server forks a child per session channel, so N muxed commands run as
N concurrent processes on the node.

The cliff at eleven is `MaxSessions`, whose default is 10 and which
`sshd_config(5)` defines as "the maximum number of open shell, login or subsystem
(e.g. sftp) sessions permitted per network connection. Multiple sessions may be
established by clients that support connection multiplexing." Verified from the
man page on the Spark itself, and reproduced directly: holding ten sleeping
sessions on a master and issuing an eleventh gives

```
debug1: auto-mux: Trying existing master at '…/cm-7e3b915…'
mux_client_request_session: session request failed: Session open refused by peer
debug1: Connecting to 192.168.29.60 [192.168.29.60] port 22.
Authenticated to 192.168.29.60 ([192.168.29.60]:22) using "publickey".
ControlSocket …/cm-7e3b915… already exists, disabling multiplexing
```

The command still succeeds. It just pays a full 500 ms handshake, and — note the
last line — the fallback connection cannot become a master either, so *every*
call pays it for as long as the master is saturated. That is the failure mode to
design against, and it is per-connection, therefore per-node, so this one-machine
measurement transfers directly. With one rank per node we are at one concurrent
session; the ceiling only matters once a node carries a persistent events tail
plus log follows plus inspects, which is precisely what recommendation 3 starts
doing. It is a ceiling of ten, and it is worth knowing where it is rather than
discovering it as a mysterious 500 ms.

(`sshd_config(5)`, https://man.openbsd.org/sshd_config.5 . The same family of
report exists in the wild for `DOCKER_HOST=ssh://`, which is why §5.2 rejects it:
https://github.com/docker/compose/issues/11677 .)

### 1.3 What sustained polling costs the node

Forty repetitions of each, with `/proc/stat` sampled on the node before and after
over a separate multiplexed channel. `clones` is the delta in `/proc/stat`'s
`processes` counter — every `fork()` and every thread the Go CLI's runtime
creates. `node CPU` is the delta in user+nice+system jiffies across all twenty
cores.

| operation | wall | clones on the node | node CPU |
|---|---|---|---|
| muxed `true` | 19.8 ms | 1.0 | 2 ms |
| muxed `docker inspect` ×1 | 30.1 ms | 10.6 | 16.8 ms |
| muxed `docker inspect` ×4, one ssh | 35.9 ms | 10.6 | 18.2 ms |
| muxed `docker ps -a --format '{{json .}}'` | 176.7 ms | 10.5 | **176 ms** |
| **un**muxed `docker inspect` ×1 | 529.8 ms | **187.1** | **238 ms** |
| **un**muxed `true` | 524.1 ms | 178.4 | 226 ms |

Three things fall out of this table.

**Sustained fan-out is free.** One rank per node, polled every ten seconds, is
1.7 ms of CPU per second per node: 0.17% of one core, 0.008% of the box. The
concern that motivated this study — that a cluster is doing inference on those
boxes — is answered, and answered in SSH's favour. It was *not* free before
multiplexing: 23.8 ms/s, 2.4% of a core, and 187 process creations per probe
rather than 11. That factor of seventeen in process creations is the Ubuntu login
path — PAM, the session setup, the motd machinery — which multiplexing skips
entirely because a session channel on an existing connection is one `fork()` in
the connection's existing privilege-separated process, not a new TCP accept, key
exchange and authentication (OpenSSH `README.privsep`;
https://github.com/openssh/openssh-portable/blob/master/README.privsep).
So §2.1a's "speed is settled" understated its own win: multiplexing bought a
14× reduction in the CPU we steal from a node running inference, not only a
12.7× reduction in latency. That is the strongest single argument in this
document for leaving the transport alone.

**Batching is a no-op for us.** Four inspects in one SSH command cost 35.9 ms and
the same node work as one — genuinely 3.3× cheaper than four separate calls. But
this hardware is one GPU per node and the plan is one rank per node
(`cluster-agent-plan.md` §3.3), so there is nothing to batch. It is one line to
add the day a node holds two ranks, and worth nothing before then.

**`docker ps` is the wrong substitute.** It costs 176 ms of node CPU against
16.8 ms for a targeted inspect — ten times more — and it does not carry
`ExitCode`, `OOMKilled`, `FinishedAt` or `Health` as machine-readable fields;
`docker ps` gives them only inside the prose of `.Status` ("Exited (137) 3
minutes ago"). Docker Engine API v1.51, `ContainerState` and `ContainerSummary`:
https://docs.docker.com/reference/api/engine/version/v1.51.yaml . It stays right
where we already use it — the list's single enumerate — and wrong for rank state.

### 1.4 What it costs this control plane

Twenty polls of four ranks, serially, over four warm masters: **125.7 ms wall per
poll and 13.0 ms of local child CPU per poll.** One `ssh` process per call is
spawned here regardless; that is the price of the subprocess transport and it is
small.

---

## 2. Where SSH actually costs something

### 2.1 A node that does not answer costs ten seconds, every poll, forever

Three shapes of absence, all with `ConnectTimeout=10`
(`spark_pulse/tools/ssh.py:59`):

| what is wrong | time to an answer | what the caller gets |
|---|---|---|
| unused LAN address, ARP fails (×3 fresh addresses) | 10.02 s | `SSHError(timeout)` |
| routed but silently dropped (192.0.2.1, 198.51.100.7) | 10.02 s | `SSHError(timeout)` |
| accepts TCP, then never speaks SSH | 10.00 s | `SSHError(timeout)` |

The third is the interesting one, and SSH handles it better than I expected:
`ConnectTimeout` covers the banner exchange, not only the TCP connect. The node
returns `Connection timed out during banner exchange`, ssh exits 255, and
`_raise_if_transport_failure` (`spark_pulse/tools/ssh.py:461`) turns that into a
structured `SSHError` rather than a command result. Credit where it is due: the
*transport* layer's unreachable-versus-failed distinction works, on evidence.

What does not work is the repetition. With a warm master and the node gone
silent, nine consecutive polls at a ten-second interval each blocked the full
10.0 s — the whole interval, every interval, for ninety seconds. Recovery, when
the node came back, was immediate: 0.5 s on the next poll. There is no state
carried between probes, so the poller pays the full detection cost again and
again for as long as the node is down, and it learns nothing in between. That is
the structural difference between poll and push, and it is measurable in seconds
rather than argued.

One related observation I could not fully explain and so state only as observed:
an established ControlMaster to a silent node was still present at 50 s and gone
at 60 s, which is the `ControlPersist=60s` idle deadline rather than the
`ServerAliveInterval=15` × `ServerAliveCountMax=3` = 45 s deadline
(`spark_pulse/tools/ssh.py:58-61`). Whether the keepalives fired and were
absorbed, or were never armed on a master with no active channels, I did not
determine.

### 2.2 The serial comprehension turns one silent rank into a ten-second request

Reproducing `status()`'s shape — four ranks, one of them silent, the shipped
ten-second `get_container_status` timeout:

| | all four healthy | one silent |
|---|---|---|
| serial, as `native_runtime.py:2040` does it | 0.13 s | **10.14 s** |
| concurrent | 0.06 s | 10.01 s |

Concurrency halves the healthy case and does nothing for the sick one, because
the timeout dominates. `GET /api/deployments/{id}` is a sync route
(`spark_pulse/routers/deployments.py:181`), so this blocks an AnyIO threadpool
worker for the duration. Two silent ranks would be twenty seconds serially and
still ten concurrently — which is the argument for doing both fixes, not one.

This also settles the fetch-on-expand decision, though not for the reason the
code comment gives. Putting rank state in the list is wrong — but because a
silent rank would hang the whole Jobs page for ten seconds on every poll, not
because four round trips are expensive. Four round trips are 41 ms. The comment
at `web/src/pages/InferencePage.tsx:93-105` should say the former.

### 2.3 Streaming, backpressure and cancellation are already available

§2.1a lists streaming as one of three things "only an agent can" do. That is not
what the transport shows.

`docker events --since 720h --filter type=container`, run through
`OpenSSHClient.remote_shell_command` on a warm master: **first bytes at 29 ms,
196 historical container events replayed** — real ones, `kill` / `stop` / `die` /
`destroy` on `spark-pulse-ed9a546f80ac-r0-g1` from our own earlier deployments —
**and the stream still open at 15 s.** A synthetic 100 ms-cadence stream over one
held channel delivered at 2.7 ms median, 16.2 ms p90, 106 ms max, with
inter-arrival tracking the source at 102.8 ms median. A held-open idle channel
cost less than the Spark's own background: 1–3 clones in a 20 s window against
background bursts of 180–370.

Two properties come with it and two caveats.

*Backpressure* exists per channel — SSH reimplements sliding windows per
multiplexed channel, which is what HPN-SSH's window-size work is about
(https://www.psc.edu/hpn-ssh-home/hpn-ssh-faq/). It is a blocked pipe rather than
an application-visible signal, which is weaker than gRPC's, but it is not absent.

*Cancellation* is arguably better than the agent's. Closing an SSH channel hangs
up the remote process; the plan's own §5 warns that "gRPC does not interrupt a
running handler, so cancellation has to be plumbed explicitly or every closed log
stream leaks a `docker logs -f`." Over SSH that leak is the default-off case.

The first caveat is replay. `docker events --since` is the documented resume
mechanism and its semantics are race-free — "Show events created since this
timestamp then stream new events" — but the buffer is capped. Docker documents
"only the last 256 log events are returned"; **measured on this Spark, exactly
256**. So an event stream must be paired with a periodic full reconcile or a long
disconnect silently loses transitions. That is not a workaround, it is what the
kubelet does: KEP-3386's evented PLEG keeps relisting at reduced frequency as the
safety net precisely because "a container would not be created, terminated, and
garbage collected within one relist period" is an assumption a pure event stream
cannot make
(https://github.com/kubernetes/enhancements/blob/master/keps/sig-node/3386-kubelet-evented-pleg/README.md,
https://github.com/kubernetes/kubernetes/blob/master/pkg/kubelet/pleg/generic.go).

The second caveat is the session budget of §1.2: an events tail holds one of the
node's ten session slots for its whole life, as does every `docker logs -f`.

### 2.4 The structural-error problem is worse than §2.1a says, and it is live

§2.1a points at `_classify_ssh_error` matching English substrings. It does, but
that is the transport layer and it only runs on exit 255 — where, as §2.1 above
shows, the exit code alone already carries the structural fact.

The live defect is one layer up. `RemoteNodeService.get_container_status`
(`spark_pulse/tools/node_service.py:659-669`) does:

```python
result = self._exec(f"docker inspect --format '{{{{json .State}}}}' {name}", timeout=10)
if not result.ok:
    return {"status": "missing", "running": False, …,
            "error": f"Container '{name}' not found on {self.node.label}"}
```

Measured on the Spark, Docker 29.2.1:

| command | exit | stderr |
|---|---|---|
| `docker inspect … no-such-container` | 1 | `error: no such object: no-such-container` |
| `docker inspect … caddy` with the daemon unreachable | 1 | `Cannot connect to the Docker daemon…` |

Both map to `missing`. `_rank_status`
(`spark_pulse/tools/native_runtime.py:2004-2022`) only reports `unknown` when an
exception is raised, so **a peer whose Docker daemon is dead reports its rank as
gone.** The rank is very possibly still serving. The control node's own rank is
told the truth, because the local service distinguishes `NotFound` from
`APIError` (`spark_pulse/tools/docker.py:777-786`).

This is the exact class of bug §3.3's "three states, not two" exists to prevent
and §2.2's "released on inference rather than on evidence" describes — the same
shape as the simulation defect phase E already found and fixed. It belongs on
§2.2's list as a sixth item. It is also the honest counter to "these only bite
above one node": this one bites at one remote node, today, and the fix is a
`stderr` test or an exit-code check in one function.

---

## 3. What a gRPC agent actually changes, for rank state specifically

Not in general. For "which rank of this cluster is unhealthy".

| property | over SSH, measured | with the agent | delta that matters |
|---|---|---|---|
| latency of a state change reaching the control plane | 2.7 ms median over a held channel; 29 ms to establish | one stream hop | none |
| N inspects → one stream | `docker events` per node, one channel | one stream per node | none in kind; the agent's is ours to define, Docker's is Docker's |
| node reports without being asked | yes, `docker events` | yes | none |
| replay after a disconnect | `--since`, bounded at 256 events (measured) | ours to design, unbounded if we want | small; both need a reconcile |
| liveness without a probe | none — 10.0 s per probe, per poll, forever | keepalive on a held stream, seconds, once | **real** |
| unreachable vs failed, at the transport | exit 255 → `SSHError` | gRPC status | none |
| unreachable vs failed, above the transport | stderr text, and today not even that (§2.4) | typed payload vs status | **real** |
| backpressure | per-channel window, no application signal | HTTP/2 flow control, application signal | modest |
| cancellation | channel close hangs up the process | must be plumbed (plan §5) | SSH wins |
| concurrency ceiling per node | 10 sessions, then 500 ms per call | one stream, many logical calls | real once a node carries a tail + logs + inspects |

Two of eleven rows favour the agent decisively. One is liveness — but note that
recommendation 3's held events channel supplies most of it: a channel that dies
tells you the node did, without a 10 s probe. What it does not supply is a
*positive* heartbeat distinguishing "nothing has happened" from "we have lost the
node", which is the reason every system in §6 sends one. The other is a typed
error above the transport, which is genuinely unavailable over a remote shell and
which §2.4 shows we are currently getting wrong.

The row that does *not* favour it, and which was the premise of this study, is
fan-out. There is no version of the rank-state problem at four nodes where gRPC
is meaningfully faster or cheaper than what we measured.

---

## 4. The costs, checked against §3.2 and §5

The plan's accounting holds up. Nothing in it is overstated, and one item is
understated.

**Enrollment** (§3.1) is eight steps with a time-boxed token, a `sudo -n` probe,
a password fallback through the SSH channel's stdin, and a distinction between
remove-and-wipe and uninstall-keep-identity. That is correct and it is a lot. All
of it is control-plane work we do not need for rank state.

**mTLS and rotation** (§3.2): a ten-year CA whose key never leaves the control
node and whose loss re-enrolls every node, a one-year server certificate rotated
hot, ninety-day agent certificates renewed at a jittered 50–80% of remaining
life, `NotBefore` backdated five minutes for clock skew, and a fallback for a
node switched off past expiry. Also correct, also a lot, and every line of it is
a thing that can break at 3 a.m. on hardware with no console.

**Packaging, upgrade and skew** (§5): "every schema change becomes a two-sided
change with a compatibility window." Worth noting the asymmetry against
recommendation 3: `docker events` is Docker's contract, versioned by Docker,
already installed, and we do not own its compatibility window. Our own proto we
would own on every node forever.

**Agent down, node up** — the failure mode SSH does not have — is the one the
plan sketches least. §3.3's three states absorb it correctly in the state machine:
`unknown` is not `dead`, and "Never kill a gang because the control plane lost
contact with a node while rank 0 is still serving." But the operator-facing
consequence is that a perfectly healthy four-node inference cluster goes amber
because one supervisor unit crashed, and the only way to find out is to go in
over SSH — which we would still have. Against that, the SSH path's equivalent
failure is that sshd is down, which also takes away the remedy. The honest
statement is that the agent adds an independent failure domain whose remedy is
the transport we already have, so the agent does not remove SSH from the system;
it adds something above it.

**The two traps in §5 are both real for this case specifically.** Our Docker SDK
is synchronous, and "one blocking call inside an asyncio handler starves every
other RPC in the process, so a slow pull looks exactly like a dead node" — note
that the current design accidentally avoids this by running the blocking work in
FastAPI's threadpool. And the cancellation trap is worse for the agent than for
SSH, per §3.

---

## 5. The alternatives, costed

### 5.1 A long-lived `docker events` stream over one SSH connection — recommended

Measured in §2.3: 29 ms to establish, 196 events replayed, 2.7 ms median
delivery, negligible held cost, 256-event replay bound, one of ten session slots.

Cost: one long-lived subprocess per node, a supervisor to restart it, a `--since`
cursor per node taken from the last event's own `TimeNano` rather than our clock
(Docker's `--since` is interpreted against the *client's* clock, so a skewed node
skews the window), and a slow reconcile behind it. Perhaps 150 lines against the
existing `EventBroadcaster` and `HealthMonitor` (`spark_pulse/tools/health.py:134`,
already a 30 s loop looking for somewhere useful to poll).

It removes the fan-out entirely: rank state stops being an inspect per rank per
poll and becomes an event when something changes, plus a reconcile.

### 5.2 `DOCKER_HOST=ssh://` — reject

Actively worse than what we do now. Docker's connection helper spawns one
`ssh … docker system dial-stdio` process per HTTP connection
(https://github.com/docker/cli/blob/master/cli/connhelper/connhelper.go,
`commandconn.New` → `exec.CommandContext`), wired to a zero-value
`http.Transport` with only `DialContext` set
(https://github.com/docker/cli/blob/master/cli/context/docker/load.go). Go's
`DefaultMaxIdleConnsPerHost` is 2 and no `MaxConnsPerHost` is set, so N
concurrent API calls become N ssh processes, N session channels, and N remote
`dial-stdio` processes. Six containers is enough to hit `MaxSessions`
(https://github.com/docker/compose/issues/11677); the accumulation case is
https://github.com/moby/moby/issues/46076 . Docker's own docs recommend
`ControlMaster` for `ssh://` contexts
(https://docs.docker.com/engine/security/protect-access/) — which is exactly the
configuration that turns unbounded connections into the ten-session wall.

It also buys nothing we lack: no typed errors, no liveness, and a second remote
binary on the hot path.

### 5.3 A single batched inspect per node — already true, and a no-op

Measured: four inspects in one SSH command cost 35.9 ms and 10.6 clones — the
same node cost as one. Genuinely 3.3× cheaper than four separate calls. And with
one GPU and one rank per node it is a no-op today. Keep it in the drawer.

### 5.4 A shorter probe timeout, and concurrency — cheapest, do it first

§2.1a proposed "a shorter `ConnectTimeout` on liveness probes specifically" and
it was never built. Measured value: the whole of §2.2's table. This is a
constant, a `ThreadPoolExecutor` and a test.

### 5.5 Rank state on the deployment list — still no

For the reason in §2.2, restated: not because 41 ms is expensive, but because
10 s × (silent ranks) is, on a poll that runs whether or not anyone is looking.

---

## 6. What the ecosystem does

Not one of the systems surveyed obtains per-container state by having the control
plane run a remote shell command per object per interval. The split is between
push-from-a-node-resident-process and a deliberately slow control-plane poll, and
the reasons they give are worth more than the mechanisms.

**Nomad** is pure push: clients "register themselves, send heartbeats for
liveness, wait for new allocations, and update the status of allocations", all
client-initiated. The server dictates the heartbeat TTL —
`max_heartbeats_per_second` defaults to 50 and exists so "the TTL [can] be
increased to meet the target rate", with `min_heartbeat_ttl` 10 s and
`heartbeat_grace` 10 s. The third state is explicit: with `disconnect.lost_after`
set, "Clients that contain `unknown` allocations will transition to
`disconnected` rather than `down`."
(https://developer.hashicorp.com/nomad/docs/architecture,
https://developer.hashicorp.com/nomad/docs/configuration/server,
https://developer.hashicorp.com/nomad/docs/job-specification/disconnect)

**Kubernetes** is push, and it split the channel when the cost showed up.
NodeStatus goes up every 10 s if changed, 5 m otherwise; a tiny Lease is renewed
every 10 s. KEP-589's stated motivation is writes, not reads: "in big enough
clusters (more than 2000 nodes)… we were hitting etcd limits for its database
size", NodeStatus objects "potentially exceeding 15kB", and the result "reducing
etcd write throughput from 150MB/min to 30MB/min". The controller side is
`--node-monitor-period` 5 s and `--node-monitor-grace-period` 40 s, after which
the Ready condition goes to `Unknown` and eviction waits a further five minutes.
(https://kubernetes.io/docs/concepts/architecture/nodes/,
https://github.com/kubernetes/enhancements/blob/master/keps/sig-node/589-efficient-node-heartbeats/README.md)

Closer to home, the kubelet's own container-state layer is the same argument in
miniature. Generic PLEG polls the runtime every 1 s; KEP-3386's motivation is
that "polling incurs non-negligible overhead as the number of pods/containers
increases", and its answer is a streaming CRI RPC — with relisting *retained* as
the safety net. That is exactly the shape of recommendation 3.

**k3s** changes only the transport, not the reporting: agents run an ordinary
kubelet, and k3s adds a websocket tunnel via `rancher/remotedialer`, whose stated
purpose is the NAT inversion — the agent dials out and the server dials back
through the same socket, so nodes need no inbound port
(https://docs.k3s.io/architecture,
https://github.com/k3s-io/k3s/blob/master/pkg/agent/tunnel/tunnel.go,
https://github.com/rancher/remotedialer). Worth noting for §8: that is a design
we can have over SSH for free, since SSH already dials out from the control plane
and multiplexes both directions.

**SwarmKit** is pure push over three gRPC channels — `Session` ("Agents should
list on the stream at all times for instructions"), `Heartbeat` (which returns
the next TTL, 5 s default, ×3 grace to `DOWN`), and `UpdateTaskStatus` ("Node
should send such updates on every status change of its tasks"). It is also the
cautionary tale the plan already cites: the deprecated full-list `Tasks` stream
was replaced by `Assignments` with `COMPLETE`/`INCREMENTAL` types and
`AppliesTo`/`ResultsIn` sequence tokens, and task states "may never move
backwards" so that duplicate and out-of-order pushes are safe. Push is not free;
it has to be made idempotent.
(https://github.com/moby/swarmkit/blob/master/api/dispatcher.proto,
https://github.com/moby/swarmkit/blob/master/manager/dispatcher/dispatcher.go,
https://github.com/moby/swarmkit/blob/master/design/task_model.md)

**Ray** is the hybrid, and its split is the one to copy: resource state is pushed
every 100 ms (`raylet_report_resources_period_milliseconds`), while **liveness is
explicitly pulled** — "The health check is done in pull based way, which means
this module will send health check to the raylets to see whether the raylet is
healthy or not" — at 3 s with a threshold of 5 failures.
(https://github.com/ray-project/ray/blob/master/src/ray/gcs/gcs_health_check_manager.h,
https://github.com/ray-project/ray/blob/master/src/ray/common/ray_config_def.h)

**Slurm** is the outlier and the most directly relevant, because its workload is
ours. The controller polls: "the `slurmctld` daemon periodically pings the
`slurmd` daemon on every configured node", at half of `SlurmdTimeout`, whose
default is 300 s — so every 150 s. The stated reason for polling that rarely is
not control-plane load but jitter: "Longer intervals decrease system noise on
compute nodes (we do synchronize these requests across the cluster, but there
will be some impact upon applications)". Fan-out is a tree, `TreeWidth` 16.
`sinfo` documents both an `UNKNOWN` state and a `*` suffix for "presently not
responding".
(https://slurm.schedmd.com/faq.html, https://slurm.schedmd.com/big_sys.html,
https://slurm.schedmd.com/sinfo.html)

Slurm's reason is the one that was raised against our design — a cluster is doing
inference on those boxes — and our measurement answers it directly: 16.8 ms of
CPU per probe per node, 0.17% of one core at a ten-second interval, against
Slurm's concern at a scale where a synchronised ping across thousands of nodes
perturbs an MPI collective. If jitter ever becomes the constraint here, the
answer it points to is a *longer interval*, not an agent.

**On "why not SSH", nobody has written it down.** Salt is the only project with a
documented head-to-head, and it is a performance claim rather than an
architectural one: "Be aware that since all communication with Salt SSH is
executed via SSH it is substantially slower than standard Salt with ZeroMQ"
(https://docs.saltproject.io/en/latest/topics/ssh/index.html). None of Nomad,
Kubernetes, k3s, SwarmKit, Ray or Slurm publishes an argument against SSH; the
evidence is structural — remotedialer's NAT inversion, SwarmKit's always-listening
session, Nomad's server-dictated TTL for rate-limiting inbound load — not argued.
That absence cuts both ways and should be reported as an absence rather than
filled in.

---

## 7. What could not be measured with one Spark

This section matters more than usual, because the question was about fan-out and
there is one machine. Be precise about which numbers are which.

**Real measurements of one remote node.** Everything in §1.1, §1.3 (per-op costs),
§2.1, §2.3 and §2.4. These are what they say they are: one control plane, one
GB10, over the LAN, with the shipped client.

**Single-node stand-ins for N nodes, and how they are bounded.**

*The concurrency tables in §1.2 put all N sessions on one Spark.* On N real nodes
the client side is identical (N ssh processes here), each node does 1/N of the
work I made one node do, and the network paths are independent rather than
sharing one link. So the measured wall time is an **upper bound** for the same N
across N nodes, and the measured per-node CPU is an **N× overstatement**. What it
cannot show is cross-node variance: one slow node dragging a gang, per-link RTT
differences, or a node whose Docker daemon is busy pulling. Those can only make
the *tail* worse, never the median better, so the direction of the error is known
even though its size is not.

*The `MaxSessions` ceiling is per network connection*, hence per node, so §1.2's
cliff transfers to N nodes unchanged. That one is not an extrapolation.

*The failure experiments used substitutes for a dying Spark.* The powered-off
case used unused LAN addresses (ARP never resolves) and routed-but-dropped
addresses. The half-open case used a local TCP relay that accepts and then holds
bytes without forwarding — which is what a node whose kernel is alive and whose
sshd is wedged does, and is **not** what a yanked cable does. A yanked cable
gives no ACKs at all and hands the problem to TCP retransmission timers, which I
did not reproduce and which will take longer than 10 s. So "10.0 s to establish
unreachable" is measured for two of the three shapes and unmeasured for the
third.

*The ControlMaster lifetime observation in §2.1* was taken through that same
relay, so it too describes the wedged-node shape only.

**Not measured at all.**

* gRPC. There is no prototype. Every number attributed to an agent in §3 is from
  the literature or from arithmetic, never from a running thing.
* A live `docker events` message produced during the measurement. Nothing was
  created, started or stopped on the Spark. The 196 events replayed were real and
  ours, produced by earlier deployments; the live-stream property is evidenced
  only by the stream staying open at 15 s and by the synthetic stream's 2.7 ms
  delivery latency.
* Anything about the rendezvous, NCCL, interface pinning or a real gang failure.
  This document is about a transport and says nothing about those; the list at
  the end of `cluster-agent-plan.md` §7 is unchanged by it.
* The cost of the recommended events tail *at four nodes over hours*, including
  what happens to a `--since` cursor across a control-plane restart.

---

## 8. If the answer becomes "build it", the smallest useful version

Not §4 phase D. That is enrollment, gRPC transport, mTLS, the ledger, packaging
and upgrade, and the whole §3.3 state machine, and it is sized for commanding
nodes rather than for observing them.

The smallest thing that buys all three of §2.1a's remaining justifications is an
agent that only *reports*, over the transport we already have:

* **One process per node, started over SSH on demand**, speaking a length-framed
  typed stream on stdio: `ssh <node> spark-pulse-agent stream`. No inbound port,
  no CA, no certificates, no enrollment token — the SSH connection is the
  authentication, and it is the same trust we already rely on for `docker run`.
  This is k3s's dial-out shape without k3s's tunnel, because SSH already is one.
* **Three message types and no more**: `hello{node_uuid, agent_version}`,
  `heartbeat{seq, boot_id}`, `state{container, rank, generation, status,
  exit_code, oom_killed, started_at, finished_at, health}`. Every command still
  travels over SSH exactly as it does today. Nothing acts on what the agent says
  except the UI and the health monitor.
* **Payload, never a status**, per §3.2: a `state` message arriving means the node
  was reachable and the outcome is definite; the stream ending means unknown.
  That is the typed error §2.4 shows we do not have, and it costs a struct.
* **The reconcile stays.** The 10 s inspect becomes a 60 s reconcile, for the
  same reason evented PLEG keeps relisting.

What this defers, and what it therefore does not have to get right first:
enrollment and its eight steps, the CA and its ten-year key, ninety-day rotation
with backdated `NotBefore`, the revocation ledger, reimaging detection,
`ENHANCE_YOUR_CALM` keepalive tuning, controller epochs and fencing, and a
command path with a compatibility window. Every one of those is a §5 recurring
cost, and none of them is needed to tell an operator which rank is unhealthy.

gRPC and mTLS then become an upgrade of the transport underneath a message
contract that already works — and one taken when there is a measurement saying
SSH's ceiling of ten sessions or its 10 s-per-probe liveness actually bit, on two
machines, rather than in advance.

---

## 9. What §2.1a should say

Keep the decision. Change three things in the reasoning.

1. **Delete "All three remaining justifications only bite at more than one
   node."** Liveness bites at one remote node — 10.0 s per probe, per poll,
   indefinitely (§2.1) — and structural errors bite at one remote node today, as
   a wrong answer in shipped code (§2.4). What is true, and is the better
   sentence, is that neither is *best* fixed by an agent.
2. **Record the failure-path measurement.** §2.1a timed a burst of eight commands
   on a healthy node and generalised. The sustained fan-out it did not measure
   turns out to favour SSH more strongly than the burst did — 0.17% of one core
   per node, and a 14× reduction in stolen CPU that the section did not claim —
   and the failure path it did not measure is where the whole cost is.
3. **Stop crediting streaming to the agent alone.** A `docker events` tail over
   one SSH channel is 29 ms to establish and 2.7 ms median delivery, and SSH's
   cancellation is better than gRPC's by default. What the agent uniquely buys is
   a typed error above the transport and a positive heartbeat.

And add to §2.2's list of things shipped and broken because multi-node never ran:
**`RemoteNodeService.get_container_status` reports a dead Docker daemon as a
missing container**, so a rank on a node whose daemon died reads as stopped rather
than unknown, while the same rank on the control node reads correctly.
