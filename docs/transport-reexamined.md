# Transport, re-examined: PAM, the aggregate, positive liveness, and the argument that is not about performance

Status: research, 2026-09-04. Audits `docs/rank-state-transport.md`, which
audited `docs/cluster-agent-plan.md` §2.1a. Two objections were raised against
that document's conclusion and three architectural questions were raised
afterwards. This answers all five.

Measurements were taken this session against the DGX Spark at `192.168.29.60`
(`gx10-ced2`, GB10, 20 cores, Linux 6.17.0-1021-nvidia, Ubuntu 24.04 userland,
OpenSSH 9.6p1 Ubuntu-3ubuntu13.16, systemd 255, Docker 29.2.1, `CLK_TCK` 100),
from this control plane over the LAN. **Every remote command was a read.**
Nothing was deployed, built, pulled or written on that machine, and
`~/projects/spark-vllm-docker` was not touched. A second, separate experiment
built four fake nodes in local containers on this Mac under podman; those were
created and destroyed here, not on the Spark.

---

## The recommendation

**Keep SSH. Do not build the agent — and this time the objections have been
tested rather than argued past.** The first objection is empirically false and
false in SSH's favour: a multiplexed session channel **does not invoke PAM at
all**. Fifteen multiplexed `docker inspect`s produced **zero**
`pam_unix(sshd:session): session opened` lines, **zero** `systemd-logind` "New
session" registrations and **zero** `session-NNNN.scope` cgroups in the node's
own journal; five unmultiplexed ones produced five of each. Every multiplexed
command runs inside the connection's single existing session, with the same
`XDG_SESSION_ID` and the same cgroup — verified for sequential commands and for
six concurrent ones. The full login path is real and expensive — **~176 forks
and ~220 ms of node CPU**, dominated by `pam_motd` re-running all fourteen
`/etc/update-motd.d` scripts, confirmed by watching `/run/motd.dynamic`'s mtime
advance on every unmultiplexed connection and never on a multiplexed one — but
we pay it **once per connection**, not once per command. The prior document's
"10.6 process creations per inspect" was never PAM. It is the Docker CLI's Go
runtime: **`true` over the same channel costs 1.06 forks and 2.0 ms.**

The second objection is right about the signal and wrong about the remedy.
Absence of an answer and a positive heartbeat *are* different, and the gap is
larger than anyone has written down: a node that goes silent costs **exactly
3.00 s per probe, on every probe, indefinitely** (128 s observed, no state
carried), SSH's own transport takes **59 s** to declare the connection dead,
and a 1 Hz heartbeat over a held channel goes quiet in **1–2 s**. Worse, this
branch has **no background liveness at all**, and never has had any:
`tools/health.py` went in `e62a408`, whose subject is "delete the monitor that
never ran". With no browser open the steady-state remote-node SSH rate is
**exactly zero**, so nothing notices a dead node until a human opens the Jobs
page and expands the right row. That is a genuine hole and it should be filled. But the heartbeat
that fills it is `while :; do …; done` over one held SSH channel, whose measured
node cost is **below the box's own background noise**. Positive liveness is a
property of holding a channel, not a property of owning the process at the other
end of it.

On the aggregate, the objection is directionally right and the prior number was
wrong. `rank-state-transport.md` measured one workload of six and reported 0.17%
of a core. The real steady state, replayed for 60 s and measured on the node, is
**51 SSH session-channels per minute per node and 1.48% of one core** — 2.70% on
rank 0's node, which also serves `docker logs --tail 1000` every two seconds.
That is 8× the published figure. It still does not move the transport decision,
because 1.5% of one core of twenty is not a reason to build a daemon. What it
does reveal is that **87% of that traffic is redundant**: the log SSE loop and
the engine-metrics route each re-run the entire rank fan-out on every tick to
read a field neither of them asked for.

**The three architectural questions raised afterwards are stronger than either
performance objection, and the first of them turns out to be urgent rather than
architectural.** Auditing the seam between `DockerService` and
`RemoteNodeService` for this document found **thirty semantic divergences across
fifteen methods, three of them live bugs**. The worst is not subtle: the local
`stop_container` stops **and removes** (`docker.py:735-746`), the remote one
issues `docker stop` and nothing else (`node_service.py:666-679`) — the string
`docker rm ` appears zero times in that file — while both docstrings promise
removal, and `_is_confirmed_gone` accepts only `missing`. So a peer's container
stays in `exited` forever, `_confirm_gone` spins for its full 30 s, and **every
multi-node teardown leaks an orphan record and holds its port range, per rank,
permanently.** The daemon-versus-missing bug also has an unfixed twin three
methods away: `list_managed_containers` returns `[]` when the node did not
answer (`:865-866`) — the same "we learned nothing, therefore there is nothing"
inference, in the class whose own docstring forbids it. Both verified in the
source for this document. Neither is a transport problem; both are a
two-implementations problem.

Moving the control plane off the Spark is **28 HARD co-residency sites, of which
9 are deleted by that same unification and 19 are not touched by it** — the
container plane is a refactor of about forty lines, and the host plane (GPU,
disk, `/proc`, the HF cache, the registry address, `readiness_url`'s
`127.0.0.1`, the systemd unit, and a registry schema that fuses "control plane"
with "node") is a rewrite. And the strongest of the three — "an agent makes
multi-node testable in CI" — is the one the measurement settles, in the opposite
direction: **four SSH-reachable fake nodes were built and exercised this session
in 96 lines of Dockerfile and shell and an 11-second image build**, and they
reproduce on demand the exact daemon-dead-versus-container-missing bug we just
fixed. An agent does not remove the need for that fake; it adds a binary of ours
that must run inside it.

So `rank-state-transport.md`'s conclusion **stands**, its headline number was
wrong by 8×, and its §8 "smallest useful agent" should be **struck**, because
everything §8 was buying is now measured to be available over the transport we
already have. What should be built instead, in this order — the order changed
once the seam audit came back:

1. **Fix the three live seam bugs, then collapse `DockerService` and
   `RemoteNodeService` into one implementation over two transports** (§5.1).
   Every multi-node teardown is currently leaking. The contract test covers 6 of
   15 methods and 27 of the 30 divergences live in the untested 9. Unify on the
   Docker Engine API — measured at **4.0 ms on the node against 11.0 ms for the
   CLI**, and **7.7 ms / 2.1 forks over SSH against 16.0 / 11.55** — so the
   uniform path is also the fast one, over a contract Docker versions for us.
2. **Stop re-running `status()` from the log stream and the metrics route.**
   `sse.py:78-82` and `routers/deployments.py:209` call
   `deploy_dispatch.get_deployment` every 2 s and 5 s to read one string.
   Together they are 42 of the 48 inspects/min/node — seven-eighths of the whole
   aggregate, for nothing.
3. **One held channel per node carrying a heartbeat and a `docker events`
   tail.** Measured: heartbeat silence in 1–2 s against 59 s for transport death
   and 3 s-per-probe-forever for the poll; a held tail costs less than the
   node's own idle noise. This is the positive liveness the second objection
   asks for, and the background liveness this product has never had.
4. **Keep the containerised fake-node harness** in CI as the multi-node oracle
   the plan's §9 says does not exist. It moves §7's unproven item 5 off the list
   outright and three more partly.

None of the four needs a second Spark, a CA, an enrollment token or a proto. All
four are smaller than phase D, and the first one is smaller than this document.

---

## 1. What a multiplexed channel actually costs the node

The objection was precise, so the answer is too. Every claim in this section is
a measurement on `gx10-ced2`, not an inference from OpenSSH's source.

### 1.1 PAM: does a multiplexed exec open a session?

The node's session stack is not small. `/etc/pam.d/sshd` runs, in order,
`pam_selinux(close)`, `pam_loginuid`, `pam_keyinit`, then `common-session`
(`pam_permit`, `pam_umask`, `pam_unix`, **`pam_systemd`**), then `pam_motd`
twice, `pam_mail`, **`pam_limits`**, `pam_env` twice, and `pam_selinux(open)`.
`/etc/update-motd.d/` holds **fourteen** scripts, including
`50-landscape-sysinfo`, `50-motd-news`, `60-nvidia-health`,
`90-updates-available` and `91-contract-ua-esm-status`. If that stack ran per
command, the objection would be right and the margin would be large.

It does not run per command. Three independent measurements say so.

**The node's own journal.** Fifteen multiplexed `docker inspect`s in one time
window, then five unmultiplexed ones in the next:

| window | `pam_unix(sshd:session): session opened` | `systemd-logind: New session` | `Started session-NNNN.scope` |
|---|---|---|---|
| 15 multiplexed inspects | **0** | **0** | **0** |
| 5 unmultiplexed inspects | **5** | **5** | **5** |

The unmultiplexed window's journal is the full ceremony, five times over:
`Accepted publickey` → `pam_unix(sshd:session): session opened` →
`systemd-logind: New session 2245 of user alex` → `Started session-2245.scope`
→ … → `session closed` → `Removed session 2245`. The multiplexed window contains
none of it.

**Session identity from inside the command.** Five consecutive multiplexed
execs all reported `XDG_SESSION_ID=2134` and
`0::/user.slice/user-1000.slice/session-2134.scope`. Two consecutive
unmultiplexed execs reported sessions 2136 and 2137 in two distinct scopes.
Session 2134 is the *master's* session, opened once when the ControlMaster was
established.

**Concurrency.** Six simultaneous multiplexed commands, observed from a seventh:
one logind session (2134), one cgroup scope, one `sshd: alex@notty` process with
six direct children.

```
3853519 2300271 Ss   sshd: alex [priv]
3853687 3853519 S    sshd: alex@notty
3879649 3853687 Ss   sleep 4
3879650 3853687 Ss   sleep 4     … six in total, one parent, one session
```

**And the motd, which is where the money is.** `/run/motd.dynamic`'s mtime is
unchanged by a multiplexed exec and advances on *every* unmultiplexed
connection (20:30:09 → 20:30:24 → 20:30:25 across one muxed and two unmuxed
commands). Fourteen shell scripts re-run per connection is the most plausible
home for the ~176 forks of §1.2 — I did not isolate it by disabling `pam_motd`,
which would have meant editing the node, so treat the attribution as strongly
indicated rather than proven. What *is* proven is that a multiplexed command
does not trigger it.

### 1.2 The ladder, in forks and node CPU

100 repetitions for the multiplexed arms, 20 for the unmultiplexed ones, with
`/proc/stat`'s `processes` and busy-jiffy counters read over a separate
multiplexed channel and idle controls of matched shape subtracted. `CLK_TCK` is
100, so a jiffy is 10 ms. The node's background fork rate is bursty (0 to
12/s across five 30 s idle windows), which is why the unmultiplexed arms carry
±3% and the multiplexed arms are clean.

| per command | wall (median) | forks on the node | node CPU |
|---|---|---|---|
| **floor** — one long-lived process reading commands on stdin | **5.99 ms** | **0.005** | **0.55 ms** |
| multiplexed `true` (new session channel) | 17.7 ms | 1.06 | 2.0 ms |
| multiplexed `curl --unix-socket` container inspect | 27.0 ms | 2.10 | 7.7 ms |
| multiplexed `docker inspect` (the CLI, as we ship it) | 28.3 ms | 11.55 | 16.0 ms |
| **un**multiplexed `true` | 528 ms | **~176** | **~220 ms** |
| **un**multiplexed `docker inspect` | 549 ms | ~192 | ~242 ms |

Read the top and bottom rows together. The session work the objection asked
about is the difference between rows 2 and 5: **175 forks and 218 ms of node
CPU**. It is enormous, it is exactly the PAM/logind/motd cost, and multiplexing
removes all of it. The difference between the agent's theoretical floor and what
we ship is the difference between rows 1 and 2: **one fork and 1.5 ms**.

### 1.3 Where the 28 ms actually goes

Decomposed by measuring each layer separately:

| component | cost | how it was measured |
|---|---|---|
| local `ssh` binary spawn on the control plane | 5.2 ms | 30× `ssh -V` |
| new session channel + remote fork | ~6.5 ms | muxed `true` (17.7) − persistent-channel noop (6.0) − local spawn |
| `docker inspect` running on the node | **11.0 ms** | 40 reps timed on the node itself |
| network, pipes, residual | ~5.6 ms | remainder |

**The transport is not the cost. The Docker CLI is.** Of the 16.0 ms of node CPU
a rank inspect burns, 2.0 ms is SSH and 14.0 ms is the CLI. Measured against the
same daemon through its own socket, `curl --unix-socket .../containers/caddy/json`
costs **4.0 ms on the node** where the CLI costs 11.0 ms, and **7.7 ms / 2.1
forks** end-to-end over SSH where the CLI costs 16.0 ms / 11.55 forks.

This matters for the agent's case more than anything else in this document. An
agent holding an open Docker API client would pay the 4 ms, not the 11 ms — a
real saving, **and one that has nothing to do with the transport.** We can have
it over SSH tomorrow by making the remote command talk to the socket instead of
spawning the CLI. (Caveat: that trades a guaranteed-present `docker` binary for
a `curl` that happens to be present on this Spark. It should be an option, with
the CLI as fallback, not an assumption.)

### 1.4 Is there an SSH configuration that avoids the session work?

Yes, and it was measured: **hold one channel open and write commands to it.**
`ssh <node> bash -s`, then 200 no-ops down the pipe:

* **1 fork total on the node** (and that one was the counter read — `true` is a
  shell builtin, so the transport created none),
* **11 jiffies for all 200 calls → 0.55 ms per call**,
* **5.99 ms median round trip**, against 17.7 ms for a fresh multiplexed
  channel, because it skips both the local `ssh` spawn and the channel open.

That is the agent's per-call profile — a message on an already-open stream —
obtained with no agent. `Subsystem` buys nothing here: a subsystem request *is* a
session channel and costs the same channel open; the saving comes from opening
the channel once, not from what you ask it for.

The honest cost of that shape is what a persistent remote shell always costs:
you own framing, you own a supervisor to restart it, you own recovery when it
dies mid-command, and a wedged remote process is a resource leak rather than a
process that exits. This is exactly the design the prior document's §8 proposed
as "the smallest agent", and the finding here is that **the interesting part of
§8 was never the agent — it was the held channel.**

### 1.5 The session ceiling, and why it is the number to design against

`MaxSessions` is not set in `/etc/ssh/sshd_config` or `sshd_config.d/` on this
Spark, so it is the OpenSSH default of 10. Reproduced: ten sleeping sessions
held on the master, then an eleventh command:

```
mux_client_request_session: session request failed: Session open refused by peer
debug1: Connecting to 192.168.29.60 [192.168.29.60] port 22.
ControlSocket …/cm-7e3b915… already exists, disabling multiplexing
```

**1197 ms**, and the fallback connection cannot become a master either, so while
the master is saturated *every* call pays a full handshake — which, by §1.2, is
176 forks and 220 ms of node CPU each. At the aggregate rate of §2 that would be
48 × 220 ms = **10.6 s of node CPU per minute, 17.6% of one core**, against
1.48% today. The PAM cost the objection was reaching for is real; it is simply
not on the path we are on, and this is the failure mode that would put us there.

The same cliff is waiting in `ssh.py:164-182`: when the control-socket path will
not fit in `sun_path`, `ensure_control_dir()` returns `None` and multiplexing is
**silently disabled** for the whole process. That is a one-line configuration
accident that costs a 100× increase in the CPU we steal from a node running
inference, and it currently produces no warning.

---

## 2. The aggregate, not one workload

`rank-state-transport.md` measured rank status alone, at one inspect per rank
per ten seconds, and published 0.17% of a core. That was one of six paths.

### 2.1 Every control-plane → node interaction on this branch

Audited across `tools/node_service.py`'s call sites, the routers, `sse.py` and
`app.py`'s lifespan. "Remote commands" means per **peer** node; every method
first branches on `node.is_self` and issues zero SSH for the control node.

| interaction | where | remote cmds per node | cadence | background? |
|---|---|---|---|---|
| rank status → `get_container_status` | `native_runtime.py:2104` ← `routers/deployments.py:182` | 1 inspect (2 if the daemon is dead) | 10 s, `RANK_POLL_MS`, `InferencePage.tsx:32` | no — expanded **running** row only |
| **engine-metrics route** re-runs `status()` | `routers/deployments.py:209` | **1 inspect** | **5 s**, `METRICS_POLL_MS`, `InferencePage.tsx:37` | no — any expanded row |
| **log follow** re-runs `status()` | `sse.py:78-82` | **1 inspect** | **2 s**, `sse.py:91` | no — one SSE per expanded row |
| log follow, the logs themselves | `sse.py:70-74` | 1 `docker logs --tail 1000` | 2 s | rank 0's node only |
| deployment list | `native_runtime.py:2142` | **0** — control node only (`_rank_is_here`) | 10 s / 15 s | — |
| orphan sweep | `native_runtime.py:2250` | 1 per outstanding orphan | piggybacks the list | only if orphans exist |
| engine-metrics **sampler** | `engine_metrics.py:605` | **0 SSH** — `httpx` to loopback | 5 s, `SAMPLE_INTERVAL_SECONDS` | yes, continuous |
| OCI updater | `oci_registry.py:132` | **0** — HTTPS to registries | 900 s | yes, continuous |
| `reconcile_all()` | `app.py:179` | **0** — control node only | startup | — |
| node registry / `register_self` | `node_registry.py:471` | **0** — a JSON file, no loop, **no heartbeat** | startup | — |
| mDNS discovery | `discovery.py` | **0 SSH** — zeroconf | startup + on demand | — |
| pre-flight | `preflight.py:553` | **≈13** (9 probes + image + 3 model) | on Preview / on deploy | no |
| image sync | `images.py:693` | 1, or 3 when it pulls | user action | no |
| model replication | `models.py:1071` | ≈6–7 + rsync, then 1 progress probe / 5 s | user action | no |
| deploy: reap, create, mods, readiness | `native_runtime.py:1379,1648,1304` | ≥6 per rank, plus a 2 s readiness poll and up to 60 confirm-gone inspects | per deploy | background **thread**, for the deploy's duration |
| teardown | `native_runtime.py:1456` | 1 stop + confirm-gone at 0.5 s to 30 s | per stop | no |
| health monitor | — | **does not exist** — `tools/health.py` deleted in `e62a408`, "the monitor that never ran" | — | — |
| git-update timer | — | **does not exist** — `GIT_UPDATE_*` is unreferenced (CLAUDE.md is stale) | — | — |

Two things fall out before any arithmetic.

**With no browser open, the steady-state remote-node SSH rate is exactly zero.**
The only continuously running background work is the engine-metrics sampler
(HTTP to loopback) and the OCI updater (HTTPS to registries). Neither touches a
peer. `tools/health.py` was deleted in `e62a408` — whose subject is "delete the
monitor that never ran" — so there is nothing that periodically asks a node
whether it is alive, and on the evidence of that commit there never was.

**The engine-metrics endpoint is the largest single SSH consumer, and it issues
no SSH of its own.** `GET /api/deployments/{id}/metrics` calls
`get_deployment` — the full rank fan-out — purely to decide whether to 404,
then returns an in-memory window. The log SSE loop does the same thing every two
seconds to read `dep["status"]`.

### 2.2 What one node actually pays, replayed and measured

The worst realistic steady state is one browser on the Inference page with one
deployment expanded and the log pane open. Replayed against the Spark for 60 s
per arm, with an idle control of the same length subtracted (background was
2.617% of one core):

| arm | ssh calls/min | node CPU | net of background |
|---|---|---|---|
| idle control | 0 | 2.617% of a core | — |
| **peer rank node** — inspect @5 s + @10 s + @2 s | **51** | 4.097% | **1.48% of one core** |
| **rank-0 node** — the above plus `docker logs --tail 1000` @2 s | **82** | 5.32% | **2.70% of one core** |
| rank poll only, nothing expanded | 7 | 2.665% | 0.05% (noise) |

Cross-check against §1.2: 51 × 16.0 ms = 816 ms/min = 1.36% of a core. Measured
1.48%. The two agree.

### 2.3 One, two and four nodes

**Per node the rate does not scale with node count.** Each `status()` call
inspects every rank, so each node receives exactly one inspect per poll however
many nodes there are. What scales is the control plane's own side.

| | 1 remote node | 2 nodes | 4 nodes |
|---|---|---|---|
| SSH session-channels **per node** / min | 51 | 51 | 51 |
| node CPU **per node** | 1.48% of a core | 1.48% | 1.48% |
| forks **per node** / s | ~12.7 | ~12.7 | ~12.7 |
| local `ssh` processes / s on the control plane | 0.85 | 1.7 | **3.4** |
| local CPU on the control plane (5.2 ms/spawn) | 0.44% of a core | 0.9% | **1.8%** |
| peak concurrent sessions per node | ~3 of 10 | ~3 of 10 | ~3 of 10 |

So the answer to "is the sum still 0.17% of a core, or something quite
different?" is: **it is 1.48% of a core, 8× the published figure, and flat in
the number of nodes.** The prior document was wrong, and the correction does not
reach the threshold where a daemon becomes the cheaper answer. For scale, the
node's own idle background is 2.6% of a core — the entire control plane, at four
nodes, with a page open, costs each node **less than that node spends doing
nothing.**

Two caveats on those numbers. The fork rate, ~12.7/s, is comparable to the
box's own bursty background (0–12/s), which is a fair description of "small" but
worth saying out loud on a machine doing inference. And 87% of it is
addressable: fix the two `get_deployment` calls and the per-node rate falls from
51/min to 7/min — 0.19% of a core, which is very nearly the number
`rank-state-transport.md` published for a reason it had not established.

### 2.4 What this measurement cannot tell you

The control plane here is on **WiFi**: eight pings gave min 4.7 ms, mean 67 ms,
max 145 ms. Every wall-clock figure in this document therefore has a tail that a
wired cluster would not have; the medians are sound, the p90s are not the
hardware's. Node-side CPU and fork counts are unaffected — they are read from
`/proc/stat` on the node.

---

## 3. What positive liveness buys, concretely

### 3.1 The three detection times, measured

A local TCP relay stood in for a node that accepts TCP and then forwards
nothing — a kernel that is alive with a wedged or dead `sshd`. This is **not** a
powered-off node and **not** a yanked cable; see §7.

With a warm master, `STATUS_PROBE_TIMEOUT = 3` (`node_service.py:83`) and a
5 s poll, across 128 s of a silent node:

| signal | time to know | shape |
|---|---|---|
| healthy probe | 33–117 ms | — |
| **a poll** | **3.00 s, on every probe, forever** | 26 consecutive probes, all exactly 3.00 s, all `TIMEOUT` |
| **SSH's own transport death** on a held channel | **59.3 s** (two runs: 58.6 s, 59.3 s) | EOF on the channel |
| **a 1 Hz heartbeat** over that same held channel | **1–2 s** | the beats stop |

The poll row is the structural point restated with a smaller constant than the
prior document's 10 s: **no state is carried between probes.** The poller pays
full price every interval for as long as the node is down and learns nothing in
between. The 59 s row is what "absence of an answer" costs when you let SSH
detect it rather than asking. The 1–2 s row is what a positive signal costs, and
it was produced by a shell loop.

### 3.2 What the control plane would do differently

Not in the abstract. Three decisions that exist in this code today.

**The bug just fixed — dead daemon versus missing container.**
`get_container_status` now runs `docker version` when an inspect fails
(`node_service.py:689-734`, `DAEMON_PROBE_COMMAND` at line 92) and returns
`unknown` rather than `missing` when the daemon is silent. That fix costs a
**second remote command on the failure path**, and it only distinguishes two of
three cases: it cannot separate "daemon dead" from "node dead", because both
make `docker version` fail. A node reporting its own state does not need the
probe at all — a `state` message arriving *is* the evidence that the node was
reachable and the answer definite; a missing message is `unknown` by
construction. That is the prior document's "payload, never a status" and it is
the one property genuinely unavailable from a remote shell's exit code. **A
heartbeat over a held channel supplies it too**, and for the same reason: the
beat arriving is positive evidence of the node, and the container state riding
alongside it is positive evidence of the container.

**Gang teardown's confirmed-gone decision.** `_confirm_gone`
(`native_runtime.py:1361-1376`) polls `get_container_status` every 0.5 s for up
to 30 s, and `_is_confirmed_gone` accepts only `missing`. Against an unreachable
node that is 60 inspects, each costing 3 s once the node is silent — the loop
cannot finish, the rank is recorded as an outstanding orphan, and its GPU and
ports stay held. That is the correct answer (the plan's §3.3: release on
evidence, never on inference), and it is expensive to reach. A held channel
turns it into: the channel died, the node is `unknown`, stop asking. The
resource still is not released — it must not be — but the control plane stops
burning a 3 s probe every half-second to re-learn a fact it already has.

**Noticing at all.** This is the sharpest one. With `tools/health.py` deleted
and `node_registry` carrying no heartbeat (no thread, no loop, no SSH import),
**nothing on this branch notices a node is gone unless a human is looking at the
right page with the right row expanded.** Time to detection with the Jobs page
closed is unbounded. The plan's §3.3 timing table — suspect at 15 s, unreachable
at 60 s, gang failed at 120 s — is not implemented at any interval, because the
component that was going to implement it no longer exists. A held heartbeat
channel per registered node restores that table for the cost of one session slot
and a shell loop.

### 3.3 What it costs

A `docker events` tail held open for 45 s, against an idle control of the same
length:

| | forks on the node | node CPU |
|---|---|---|
| held `docker events` tail, 45 s | 186 | 15.78 ms/s |
| idle control, 45 s | 548 | 28.22 ms/s |

The held tail's window is *quieter than the idle window*. The measurement's
honest reading is that the cost is **below the resolution of this box's own
background noise** — it cannot be resolved at 45 s, which is a stronger
statement than a small number would be.

The real costs of a held channel are not CPU. They are: one of the node's ten
session slots per stream, per node; a supervisor to restart it; a `--since`
cursor for the events tail taken from the last event's own `TimeNano` rather
than our clock, because Docker interprets `--since` against the *client's*
clock; a periodic reconcile behind it, because Docker's event replay buffer is
capped (documented at 256, measured at exactly 256 by the prior study); and the
discipline to keep total held streams well under ten per node.

---

## 4. Costing the smallest honest agent

Three options, priced against each other. The prior document's §8 sketched the
middle one; this section is why it should be struck.

| | **held SSH channel** (recommended) | **§8 report-only agent** over SSH stdio | **phase D: gRPC + mTLS** |
|---|---|---|---|
| what runs on the node | `sshd` (already there) + a shell loop | one binary of ours, launched over SSH | one daemon of ours, enrolled, certificated, supervised |
| per-call node cost | **0.005 forks, 0.55 ms** (measured) | same — same channel | ~0 + our process's own footprint |
| positive heartbeat | **yes** (measured, 1–2 s) | yes | yes |
| typed payload vs transport status | partly — a framed JSON line is ours to define; the *transport* error is still ssh's exit 255 | **yes** | yes |
| liveness independent of a probe | **yes** | yes | yes |
| we own a wire contract | one line format, breakable at will, no installed base | a proto and a compatibility window on every node | same, plus a service contract |
| new failure mode: agent down, node up | **none** — nothing of ours runs there | **yes** | **yes** |
| CA, enrollment, rotation, revocation, epochs | none | none | 8 enrollment steps; 10 y CA, 1 y server cert, 90 d agent certs renewed at a jittered 50–80% of life, `NotBefore` backdated 5 min; a revocation ledger; controller epochs |
| packaging, upgrade, skew | none | a binary to ship to every node and version | same |
| to build | ~150 lines against the existing `EventBroadcaster` | §8's three message types, a supervisor, an installer, a version check | the plan's whole phase D |
| CI cost | **already done** — §6 | a fake node **plus** our agent inside it | same |

The row that decides it is **"agent down, node up"**. It is a failure mode
neither SSH nor the control plane has today, its remedy is SSH — which we would
therefore still be running — and its operator-facing consequence is that a
healthy four-node inference cluster goes amber because a supervisor unit
crashed. The plan's §3.3 absorbs it correctly in the state machine (`unknown` is
not `dead`), and that is the point: the state machine has to grow a case *for a
problem we would be introducing*.

The §8 agent buys exactly one thing over the held channel: a typed error above
the transport, guaranteed by a compiled contract rather than by a line format we
agree with ourselves. It costs a binary on every node, a version skew policy,
and the "agent down" failure domain. **A framed JSON line over a held SSH
channel buys 90% of it for none of that**, and the remaining 10% — a transport
error that no layer above `ssh.py` has to read as English — is already handled
structurally for exit 255 by `_raise_if_transport_failure` (`ssh.py:461`).

The gRPC/mTLS version is the plan's phase D and nothing measured here moves it
closer. It should stay where §2.1a put it: revisit when a second Spark makes the
failure modes reproducible — with the amendment of §6, that a *container* now
makes most of them reproducible without a second Spark.

---

## 5. The architectural arguments, which are not about performance

Three questions were raised after the measurements, on the grounds that they may
matter more. Two of them do. None of them is an argument for a daemon.

### 5.1 A uniform transport at size one

The plan's §7 surveyed this and reached the opposite of what we built:

> More useful still, none of them selects a local implementation per call.
> Swarm's manager runs its own agent over a unix-socket loopback transport
> speaking the identical protocol. k3s aims the ordinary remote join path at
> localhost. Nomad seeds the client's server list with the local address so a
> colocated client goes over loopback like any other.

And in the same section, about the pattern it was replacing:

> The present pattern, where every call site passes a host that defaults to
> empty meaning local, is a defect generator rather than a defect, and it has
> already fired.

The defect that fired is documented in §2.2 and quantified in
`tests/test_container_service_contract.py`: "thirteen call sites passed the
empty string and silently drove the control plane's own daemon", including "the
health check that compares NCCL settings across nodes compares the local node
with itself."

**Is `service_for()` the same shape wearing better clothes?** Yes, and worse
than that: the audit run for this document found **thirty semantic divergences
across the fifteen interface methods**, and **three of them are live bugs
today**, one of which breaks every multi-node teardown. This section is no
longer an architectural argument. It is a defect report.

The empty-host pattern's defect was that the *caller* chose the implementation
implicitly, at every call site. `service_for()` fixes that much: resolution
happens once and the call sites cannot get it wrong. What it does not fix is
that the two implementations speak **different languages about the same facts**.
`DockerService` talks to the Docker SDK and gets Python objects and typed
exceptions. `RemoteNodeService` runs the Docker CLI and gets text and exit
codes. Every method that exists twice is a place where the two can disagree, and
the disagreement is invisible on the machine we develop on, because the control
node always takes the local branch.

**Three that are broken right now**, each verified directly in the source for
this document rather than taken on report:

**(a) `stop_container` does not remove on a peer, so every multi-node teardown
leaks an orphan and holds its ports.** Both docstrings promise the same thing.
`docker.py:735-746` — "True if the container was stopped and removed" — does
`container.stop(timeout=...)` **then `container.remove(force=True)`**.
`node_service.py:666-679` — "Stop **and remove** a container on the node" —
issues `docker stop -t {timeout} {name}` and nothing else. The string
`docker rm ` appears **zero** times in `node_service.py`. Containers are created
with `auto_remove=False` (`native_runtime.py:1704`), so nothing removes it
later. Then `_teardown_entry` calls `_confirm_gone`, which polls for
`_is_confirmed_gone` — and that predicate accepts **only `missing`**
(`native_runtime.py:1345-1358`), by a deliberate design its own docstring
defends at length: "Releasing on inference rather than on evidence is the
failure the orphan machinery exists to prevent." A stopped-but-present container
reports `stopped`, forever. So `_confirm_gone` spins for
`CONFIRM_GONE_TIMEOUT = 30.0` s, fails, and the rank is recorded as an
outstanding orphan whose ports `sweep_orphans` can never release, because that
too needs `missing`. **Per rank, per teardown, on every peer.** The orphan
machinery is working exactly as designed; it is being fed a wrong answer by the
seam.

**(b) The bug we just fixed has an unfixed twin three methods away.**
`list_managed_containers` (`node_service.py:865-866`) is:

```python
result = self._exec(f"docker ps --all{filter_args} --format '{{{{json .}}}}'", timeout=10)
if not result.ok:
    return []
```

A node whose daemon is dead, or whose command failed for any reason, reports
**no containers** — which is precisely the "we did not learn anything, therefore
there is nothing" inference that `_inspect_failed_status`'s own docstring
forbids and that `5fca3fb` was written to eliminate. It has no daemon probe. It
feeds `reconciliation.py:224, 279, 373` and `native_runtime.list_deployments`.
The local implementation lets the exception out instead.

**(c) A mod with a subdirectory works locally and fails on every peer.**
`docker.py:883` shells `docker cp`, which copies directories.
`RemoteNodeService.copy_to_container` (`:818`) calls `OpenSSHClient.copy`, whose
`_build_scp_args` (`ssh.py:404-406`) is `["scp"] + self._common_options()` —
**no `-r`**. `_apply_mods` (`native_runtime.py:1241-1243`) copies every entry of
a mod directory.

**And twenty-seven more.** Among the ones that will bite: `stop_container`
aside, `pull_image` on a peer ignores `cancel`, `interval` and `stall_timeout`
entirely (`node_service.py:986-1008`), so `start()`'s `except PullCancelled`
handler can only ever fire for the control node and **a teardown during a peer's
image pull is recorded as `error` rather than `stopped`** — the exact
miscategorisation that handler exists to prevent. `list_managed_containers`
normalises a peer's status to `running` or `stopped` only
(`_normalize_cli_status`, `:393-396`), while the local path returns Docker's
full vocabulary — so `reconciliation.py:382`'s `status != "exited"` test can
**never** match on a peer. Container ids are 64 hex locally and 12 hex from
`docker ps --format json`, so any cross-node id comparison silently fails.
`exec_in_container`'s `timeout` is documented as ignored locally
(`docker.py:806-816`) and bounds the call at 30 s on a peer. `image_exists`
swallows every exception locally and raises `SSHError` on an unreachable peer,
so "cannot ask" starts a pull on the control node and skips it on a peer.
`list_images` hardcodes `"size_bytes": 0` for peers.

**The contract test that exists to catch this covers 6 of the 15 methods**
(`run_container`, `list_managed_containers`, `exec_in_container`,
`stop_container`, `image_exists`, `pull_image`);
`test_every_implementation_offers_the_whole_interface` only checks `hasattr`.
Twenty-seven of the thirty divergences live in the nine methods with no
behavioural contract test at all — including (a) and (c) above.

Three of the divergences are **contradicted by a docstring in the same file** —
`stop_container`'s "and remove", the memory-swap derivation which
`node_service.py:401-405` claims happens "on both the SDK and CLI paths" and
which the CLI path does not do, and `service_for`'s "resolution happens once"
against fifteen in-method branches. That is the signature of a design whose two
halves are kept in agreement by reading each other's prose.

So the plan's §7 was right and we did the thing it said nobody does. The
present shape is a better-guarded defect generator than the empty-host default,
and it is still firing.

**What Swarm's loopback pattern would cost us.** Priced two ways.

*Loopback SSH*, the literal reading: it cannot be measured on this Spark,
because the node has no key authorizing login to itself — the attempt returned
`unavailable: no self-authorized key`, which is itself the finding. Making the
control node a peer over SSH means installing a key on the box that authorizes
login to the box, which is a real security decision and not a refactor. From the
peer numbers less the LAN RTT it would cost roughly **12–14 ms and one fork per
local operation** that currently costs neither. That is affordable at 7 calls
per minute and wasteful at 51.

*One implementation over two transports*, which is what Swarm actually gets from
a unix socket and what we could get without a self-key: keep one node service
whose methods build one command vocabulary, and vary only how the bytes travel —
`subprocess.run` locally, `ssh` remotely. Uniformity of semantics, no loopback
authentication, no added latency. The measured price is that the local path
gives up the Docker SDK for the CLI: **11.0 ms per inspect instead of an
in-process call.** At the corrected aggregate of §2.2 that is under 1% of a core
on the control node, and if it is ever too much, the same measurement points at
the answer — the Docker **Engine API over the unix socket, 4.0 ms**, which is
also what the remote path can speak (7.7 ms and 2.1 forks over SSH, against
16.0 ms and 11.55 for the CLI).

That is the version worth building: **one vocabulary — the Docker Engine API —
two transports, one parser, one error taxonomy, and a contract Docker versions
for us rather than one we version ourselves.** It eliminates the class of bug
rather than the instance, it is faster than what we ship, and it is the only one
of these three options that makes the local and remote paths *impossible* to
disagree.

### 5.2 It would let the control plane leave the Spark

The claim is that a uniform node path would let spark-pulse run on a laptop and
manage N Sparks. It would — and the count decides whether that is a refactor or
a rewrite. **The answer is both, and the split is clean: 28 HARD sites, of which
9 are deleted outright by §5.1's change and 19 are not touched by it at all.**

The 9 are the **container plane**, and they go away because there stops being a
second implementation to disagree with:

* `service_for`'s branch (`node_service.py:1070-1075`) becomes one line, and
  the **fifteen** `if self.node.is_self:` branches inside `RemoteNodeService`
  (`:527, 647, 668, 744, 784, 811, 842, 855, 894, 903, 915, 925, 947, 998,
  1039`) plus `is_local`/`_local`/`_ssh`/`__repr__` (`:462-495`) are deleted.
  That is **fifteen of fifteen interface methods** carrying the same
  conditional — the empty-host sentinel moved from an argument into a
  conditional rather than removed.
* The sentinel itself survived too: `""` is still in `LOOPBACK_ADDRESSES`
  (`node_service.py:63-65`), `rank_services._resolve` short-circuits on an empty
  address to the local daemon (`native_runtime.py:394-395`), `_rank_is_here`
  returns `True` for it (`:403-411`), and **every solo deployment's rank 0 is
  written to disk with node `""`** (`:1059`). On an off-box control plane every
  existing `deployments.json` record silently addresses the laptop's daemon.
* `preflight.probe_for`'s control-plane branch (`preflight.py:321-333`) and its
  whole `LocalHostProbe` (`:259-287`); `reconciliation`'s two competing defaults
  (`:85-111`); `routers/docker.py`'s module-level local wrappers;
  `_inspect_image`'s local-daemon assumption (`native_runtime.py:413-436`).

The 19 are the **host plane**, and there is no node abstraction for them at all:

| what | where | why the transport does not help |
|---|---|---|
| GPU, CPU, disk, `/proc` — the entire Monitoring page and `/sse/metrics` | `tools/system.py` via `routers/memory.py:11-39` (five routes) and `sse.py:12,26,32` | **not one of these takes a node.** Off-box they describe the laptop and call it the cluster |
| rank 0 reached over loopback | `native_runtime.py:1114` `readiness_url=f"http://127.0.0.1:{port}…"`, consumed at `:1334` and `:2121`; `engine_metrics.py:499-520`; `_port_free` binding `("127.0.0.1", port)` at `:460-466`; `benchmarking.py:162` | a rank on a peer is unreachable, so **every deploy hits `deploy_ready_timeout_seconds` and is marked `error` while the engine serves fine** |
| the HF cache is assumed to be **the same path on every node** | `models.py:176-183` used as the *peer's* path at `:985` and `:1249` and as the peer's `df` target via `preflight.py:2064-2068` | the control node's `$HOME` is written into commands that run on a peer |
| the control node is structurally the only replication source | `models.py:957-970` raises "Model not in local cache" | a laptop cannot seed a cluster without holding every model |
| the control node is both a preflight target and the digest reference | `preflight.py:1728-1740, 1824-1828, 1851-1885` | off-box it holds no engine image, so `control_holds_it` is False and the image-ID fallback silently never fires |
| the local Docker registry peers pull from | `registry.py:191-225`, `cluster_address()` falling back to `127.0.0.1` | peers are told to pull from a laptop, at an address no peer can reach |
| the Cache page lists and **deletes** `~/.cache/*` | `cache.py:13-60`, `routers/cache.py:12,20` | the destructive action lands on the wrong machine |
| the control node's `$HOME`/`$HF_HOME` expanded into **every rank's** mounts | `native_runtime.py:440-455, 690-707` | handed to `ensure_directories` and `run_container` on peers |
| the Images page is the control node's local image list | `images.py:104-108` driving `:282, 387, 568` | "delete image" deletes from the wrong machine |
| the systemd unit requires a local Docker daemon | `service.py:19-23` `Requires=docker.service` | the unit will not start on a machine without one |
| "control plane" and "node" are one boolean on one record | `node_registry.py:141, 283-284, 381-382`, `_UPDATABLE` at `:308-321` | **there is no way to express N Sparks, zero of which run the control plane.** A data-model change, not a code change |
| `register_self()` enrols this machine as a healthy node | `node_registry.py:471-510` via `app.py:158` | a laptop is enrolled with its **Wi-Fi interface recorded as `ethernet_interface`**, which `native_runtime.py:841-856` then pins NCCL to |

**The loopback-rank-0 row is not a future problem. It is a live bug at two
nodes.** Rank 0 need not be on the control node — `native_runtime` places ranks
by the registry — and the moment it is not, readiness, engine metrics and the
benchmark runner all aim at `127.0.0.1` and find nothing, or find a different
deployment's port. `engine_metrics.py:503-506` states the assumption in prose:
"The head rank runs with the host's network, so its API port is a host port and
the readiness probe already reaches it at `127.0.0.1`." Nothing checks it. That
is a defect worth filing independently of everything else in this document.

**The verdict.** The container plane is a **refactor** — about forty lines in
`node_service.py`, deleting 9 of 28 HARD sites and, more importantly, all thirty
seam divergences of §5.1. The host plane is a **rewrite**: nineteen sites that
need a per-node *host* facade with the same discipline `NodeService` has (the
`HostProbe` in `preflight.py` is the only prototype and it is preflight-private),
per-node filesystem-path facts, a rank-0 address that is not `127.0.0.1`, and a
registry schema where control plane and node are independent.

Two corrections to `CLAUDE.md` fall out of the audit, both of which had been
inflating the estimate: **`SPARK_VLLM_PATH` is SOFT, not hard** —
`config.py:95-116` is explicit that "nothing is executed out of it any more" and
every failure degrades to "no recipes from there" — and **there is no
custom-file symlinking in `app.py`'s lifespan any more**; the runner and the
symlinks are gone (`recipe_sources.py:21-23`).

And the point that matters for this document: **not one of those nineteen sites
becomes easier because the node runs a gRPC daemon rather than `sshd`.** The
unblocking change is one node service with one vocabulary, and then a host
facade beside it. The wire underneath both is a detail.

### 5.3 The one that may settle it: multi-node without hardware

This was put as the strongest argument for an agent, on the grounds that a
measurement cannot refute it. It can be priced, though, and pricing it settles
it the other way.

**What already exists, and is better than the framing assumes.**
`spark_pulse/mock/node_service.py` is not a stub. It runs the **real**
`RemoteNodeService` over a simulated SSH transport, so command building, label
filtering, JSON parsing and the `is_self` branch are production code and "only
the bytes on the wire are invented." It keeps a separate container store and
image store **per host**, records every command with the host it was aimed at,
and carries two mutable failure sets: `fail_hosts`, which **raises**
`SSHError(NETWORK)` exactly as `OpenSSHClient` does on exit 255, and
`daemon_down_hosts`, which returns exit 1 with Docker's real "Cannot connect to
the Docker daemon" text for every verb including `version`.
`tests/test_multinode.py` enrols four machines and asserts what is rendered,
ordered, refused and booked; `web/tests/e2e/multinode.spec.ts` does it through
the browser. So "everything multi-node is unvalidated" is too strong: the
**bookkeeping** is validated, in-process, at up to four nodes.

**What that cannot reach**, and what a container adds: a real `sshd`, a real
`ssh` client, real ControlMaster multiplexing and its `ControlPersist` window,
the `MaxSessions` ceiling, real subprocess timeouts against a real slow peer,
real OS-level concurrency across four separate machines' worth of processes,
real `scp`/`rsync`, and the actual byte-for-byte stdout of a real Docker CLI.
Every one of those is a property of the transport, which is precisely the thing
the plan's §7 item 5 lists as unproven.

**The cheap version was built and run this session.** `debian:bookworm-slim`
plus `openssh-server`, a 48-line fake `docker` that answers the verb shapes
`RemoteNodeService` actually issues, and a two-line fake `nvidia-smi`:

* **96 lines total** (15 Dockerfile, 48 fake docker, 2 fake nvidia-smi, 31
  runner), **124 MB image, 11-second build**, four containers.
* Gang start, four ranks concurrently over four multiplexed masters: **14.1 ms**.
* Rank-status fan-out across four nodes, 20 repetitions: **12.8 ms median**.
* **Daemon dead vs container missing, on demand** — the exact bug fixed in
  `5fca3fb`. With the daemon down: inspect `rc=1`, `docker version` `rc=1` →
  verdict `unknown`. With it up and the container absent: inspect `rc=1`,
  `docker version` `rc=0` → verdict `missing`. **That is a regression test for
  the bug, needing no hardware, no simulation and no agent.**
* Unreachable peer (container stopped): `rc=255`, "Connection refused", 6 ms.
* Gang teardown with one node down: three ranks confirmed exited, one `rc=255`
  and not confirmed gone — the orphan path, over a real transport.

Each container took a routable address on port 22 (`10.88.0.229`, `10.88.0.230`),
usable directly as `NodeRecord.address` on a Linux CI runner with **no transport
change at all**. One gap found: `NodeRecord` has no `port` field and
`OpenSSHClient` emits no `-p`, so port-mapped hosts (which is what macOS forces)
need a small addition. On Linux CI, they do not.

**Where each of §7's eleven unproven items falls.**

| # | item | container CI? |
|---|---|---|
| 1 | rendezvous forms across machines, either engine | **hardware** — argument *rendering* is already simulated; *forming* needs GPUs |
| 2 | interface pinning against real per-role names | **split** — the `/sys/class/net` and `/sys/class/infiniband` lookup and its find-or-fail are software and a container has real interfaces; whether a real Spark's RoCE name is right is hardware |
| 3 | both RoCE twins of a QSFP port reaching full bandwidth | **hardware** |
| 4 | the two NVIDIA NCCL settings behaving as documented | **hardware** |
| 5 | **how an unreachable peer behaves over a real SSH transport** — half-open, stale `ControlMaster`, a node answering slowly, the `ConnectTimeout` | **software, entirely.** Measured here: half-open via a relay (3.00 s per probe forever, EOF at 59 s), refused via a stopped container (6 ms), slow via a sleeping fake docker, and the master's own lifetime. **This item can leave the list without a second Spark.** |
| 6 | anything at three or four nodes | **split** — cabling and fabric are hardware; ordering, refusals, orphan bookkeeping, concurrency and the session ceiling at four nodes are software and were exercised here |
| 7 | `NCCL_IB_MERGE_NICS=0` on this fabric | **hardware** |
| 8 | whether starting workers before rank zero matters, and the startup gates | **split** — whether it *matters* is hardware; that we *do* it, in that order, with real timing and a real failing worker, is software |
| 9 | three-node ring bandwidth per pair | **hardware** |
| 10 | SGLang `--enable-dp-attention` across nodes | **hardware** |
| 11 | `NCCL_IGNORE_CPU_AFFINITY=1` on GB10 | **hardware** |

**One item leaves outright, three move partly, seven are hardware and stay.**
The banner in `web/src/lib/experimental.ts` gets shorter by one and more honest
by three.

**And an agent changes none of that arithmetic.** With an agent the CI node is a
container running *our binary* against a fake docker; with SSH it is a container
running `sshd` and `docker`, both third-party and both real. The agent does not
remove the fake — you still need something to stand in for a GPU node's Docker —
it adds a component of ours that must also be built, versioned and installed
into the harness. Its one advantage, that CI exercises our real node-side code,
is an advantage that only exists because the agent created node-side code to
exercise. Over SSH there is none.

The higher-fidelity version of the same harness, if the fake `docker` ever
proves too thin, is to run a **real** container runtime inside each fake node —
then the CLI text, the exit codes and the daemon-down case (`systemctl stop
docker` rather than a sentinel file) are real. Heavier, strictly better, and
still no agent.

---

## 6. Where the prior document was right, wrong, and should be amended

| `rank-state-transport.md` says | verdict |
|---|---|
| "multiplexing skips [the login path] entirely because a session channel on an existing connection is one `fork()`" — cited to `README.privsep`, not measured | **right, and now measured.** §1.1: zero PAM sessions, zero logind registrations, zero scopes for multiplexed commands; 1.06 forks against ~176. |
| "10.6 process creations per inspect" attributed to the probe | **right number, wrong attribution.** 11.55 forks measured, and 10.5 of them are the Docker CLI's Go runtime — `true` over the same channel costs 1.06. |
| "0.17% of one core, sustained, at a ten-second poll" | **wrong by 8×.** It measured one of six paths. The real steady state is 51 calls/min and **1.48% of a core** (§2.2). |
| "Sustained fan-out is free" | **overstated but survives.** 1.48% of a core against the node's own 2.6% background is small; it is not free, and 87% of it is redundant. |
| §2.1 "10.0 s per probe, on every poll, indefinitely" | **still true in shape, smaller in constant.** `STATUS_PROBE_TIMEOUT = 3` landed in `5fca3fb`; measured 3.00 s per probe, 26 consecutive probes, indefinitely. |
| §2.4 "a peer whose Docker daemon is dead reports its rank as gone" | **fixed** in `5fca3fb` via a `docker version` probe. Costs a second remote command on the failure path, cannot separate "daemon dead" from "node dead" — and **has an unfixed twin three methods away** in `list_managed_containers` (§5.1b), plus 29 further seam divergences the fix did not look for. |
| §2.4 "the fix is a `stderr` test or an exit-code check in one function" | **too small a frame.** It was one instance of a class. The class is thirty divergences across fifteen methods, of which three are live bugs, one of which leaks an orphan on every multi-node teardown (§5.1). |
| §2.2 "`status()` runs its per-rank inspects in a serial list comprehension" | **fixed** — `_gather_rank_statuses` (`native_runtime.py:2073-2101`), `RANK_STATUS_MAX_WORKERS = 4`, serial below two ranks. |
| §3 "liveness … the row that favours the agent decisively" | **half right.** The gap is real and larger than stated (nothing notices at all with no page open), but §3.1 measures the remedy at 1–2 s over a held SSH channel. |
| §5.1 "one long-lived `docker events` stream over one SSH connection — recommended" | **endorsed, and cheaper than it claimed.** Held cost is below the box's background noise. |
| **§8 "if the answer becomes build it, the smallest useful version"** | **strike it.** Everything §8 buys — heartbeat, typed report, no CA, no enrollment — is available from a held channel with a framed line format and no binary on the node (§4). §8's value was the held channel; the agent around it was never load-bearing. |
| §9 "delete 'All three remaining justifications only bite at more than one node'" | **endorsed**, and add: they bite at *zero* nodes now, because with no page open nothing probes anything. |

And two amendments to `cluster-agent-plan.md` itself:

* **§2.1a's "Both polling intervals we use — 30 s health checks and sub-second
  readiness polls"** describes a health check that no longer exists.
  `tools/health.py` was deleted in `e62a408` as "the monitor that never ran",
  and nothing replaced it. §2.1a's sentence describes a component that was
  already inert when it was written.
* **§7's list of eleven unproven items** should be re-grouped into three, not
  two: specified-and-implemented, documented-nowhere, and **provable in
  containers** (§5.3). Item 5 belongs in the third group and should leave the
  banner once the harness exists.

---

## 7. What one machine, and one afternoon, cannot tell you

Stated precisely, because the whole question is about more than one node.

**Real measurements of one remote node.** §1 in its entirety, §2.2, §3.1's
healthy row, §3.3. One control plane, one GB10, over a LAN, with the shipped
options.

**Single-node stand-ins, and how they are bounded.**

* *The aggregate in §2.2 was replayed against one node.* Because each node
  receives its own inspect from the same `status()` call, the **per-node** figure
  transfers to N nodes unchanged; that is not an extrapolation, it is the call
  graph. The **control-plane-side** figures in §2.3 (local `ssh` processes, local
  CPU) *are* arithmetic — 0.85/s measured, multiplied by N. They are labelled as
  such in the table and have not been observed at N > 1.
* *The `MaxSessions` ceiling is per network connection*, hence per node, so
  §1.5's cliff transfers unchanged. Also not an extrapolation.
* *The silent node in §3.1 was a local TCP relay* that accepts and holds bytes.
  That is what a node with a wedged `sshd` and a live kernel does. It is **not**
  a powered-off node (ARP never resolves) and **not** a yanked cable (no ACKs at
  all, handed to TCP retransmission timers, which will take longer than the
  numbers here). The 59.3 s transport EOF and the 3.00 s-per-probe figures
  describe the wedged shape only.
* *The four fake nodes in §5.3 are containers on one Mac*, sharing a kernel, a
  clock and a loopback network. They prove the transport's *semantics* at four
  nodes — ordering, refusal, orphan bookkeeping, the daemon-vs-missing verdict,
  the session ceiling — and prove nothing about latency, jitter, per-link RTT
  variance, or a node whose Docker daemon is busy pulling.

**Not measured at all.**

* **gRPC. There is still no prototype.** Every number in the agent column of §4
  is from the plan, from the literature, or from arithmetic. Nothing in this
  document has ever talked to a running agent.
* **Loopback SSH on the Spark**, because the node has no key authorizing login
  to itself. §5.1's 12–14 ms is peer latency less RTT, and is labelled an
  estimate.
* **The Docker Engine API as our actual node vocabulary.** §1.3's 4.0 ms and
  §1.2's 7.7 ms / 2.1 forks are `curl` against the socket, not our client
  against it, and they assume a `curl` that this Spark happens to have.
* **A live container transition during the events-tail measurement.** Nothing
  was started or stopped on the Spark. The tail's cost is measured; its delivery
  latency is inherited from the prior study.
* **The held-channel design over hours**, including what a `--since` cursor does
  across a control-plane restart, and what ten held streams per node do to a box
  under inference load.
* **Anything about NCCL, the rendezvous, RoCE or a real gang failure.** This
  document is about a transport. The seven hardware items in §5.3's table are
  unchanged by it.

**One methodological caveat that affects every wall-clock number.** This control
plane is on WiFi: eight pings gave 4.7 / 67 / 145 ms. Medians are sound; p90s
belong to the link, not to SSH. Node-side CPU and fork counts are read on the
node and are unaffected.


---

## 8. What to build

Four things, in order, none of which needs a second Spark.

**1. Fix the seam, then remove it.** In this order, because the first three are
bugs and the fourth is the design change that stops them recurring.

* `RemoteNodeService.stop_container` must remove, not only stop
  (`node_service.py:666-679`). Until it does, every peer rank teardown leaks an
  orphan record and holds its port range. One line, and a contract test.
* `list_managed_containers` must not answer `[]` for a node that did not answer
  (`:865-866`). It needs the same `_daemon_answered` probe that
  `get_container_status` grew in `5fca3fb`, or it needs to raise.
* `OpenSSHClient.copy` needs `-r` (or `copy_to_container` needs `copy_dir`), or
  mods with subdirectories keep working locally and failing on every peer
  (`ssh.py:404-406`).
* Then collapse the two implementations. **One vocabulary, two transports.** The
  vocabulary should be the Docker Engine API over the daemon's unix socket —
  4.0 ms on the node against 11.0 ms for the CLI, 7.7 ms and 2.1 forks over SSH
  against 16.0 ms and 11.55 — reached locally by a socket and remotely by
  `ssh <node> <a small client>`. It is faster than what we ship, it is a
  contract Docker versions rather than one we translate, and it makes the local
  and remote paths *incapable* of disagreeing.
* And extend the contract test from 6 of 15 methods to 15 of 15. Twenty-seven of
  the thirty divergences are in the nine methods it does not cover.

**2. Stop the two callers that re-run the rank fan-out.** `sse.py:78-82` calls
`get_deployment` every 2 s to read `dep["status"]`; `routers/deployments.py:209`
calls it every 5 s to decide a 404. Those two are 42 of 48 inspects per minute
per node. Give the log loop the status it already has from the event stream, or
a cached one; give the metrics route a existence check that does not fan out.
Per-node cost falls from **1.48% of a core to about 0.19%**.

**3. One held channel per registered node.** A heartbeat plus a `docker events`
tail on one SSH channel, feeding the existing `EventBroadcaster`
(`tools/events.py:117`), with the inspect demoted to a slow reconcile.

* It restores background liveness, which this branch does not have at any
  interval — `tools/health.py` was "the monitor that never ran" — and which the
  plan's §3.3 timing
  table (suspect at 15 s, unreachable at 60 s, gang failed at 120 s) assumes
  exists.
* It gives the positive signal the second objection asks for: **1–2 s** to
  notice silence, against 59 s for SSH's own transport death and 3 s per probe
  forever for the poll.
* Its measured node cost is below the box's own background noise.
* It needs: a supervisor per node, a `--since` cursor taken from the last
  event's own `TimeNano` rather than our clock, a periodic reconcile behind it
  because Docker's replay buffer is capped at 256, and a budget — **each held
  stream is one of the node's ten session slots** (§1.5), and exhausting them
  disables multiplexing entirely and puts us on the 220 ms-per-command PAM path.
* Frame the messages. A JSON line per event is a wire contract, but it is ours
  to break at will with no installed base, because the process at the other end
  is `docker` and a shell, not a binary we ship.

**4. Put the fake-node harness in CI.** `debian:bookworm-slim` +
`openssh-server` + a fake `docker` + a fake `nvidia-smi`; on a Linux runner each
container's own address on port 22 is usable directly as `NodeRecord.address`
with no transport change. Built and exercised for this document: 96 lines,
11-second build, four nodes, gang start in 14 ms, rank-status fan-out in 12.8 ms,
and the daemon-dead-versus-missing verdict reproducible with a `touch`.

* It is the regression test for `5fca3fb`, which currently has none on a real
  transport.
* It is the regression test for the three bugs in item 1 — in particular for
  `stop_container`, which no in-process simulation can catch, because
  `SimulatedDockerSSHClient` implements `docker rm` and the real remote path
  never calls it.
* It moves §7's unproven item 5 — "how an unreachable peer behaves over a real
  SSH transport" — off the list outright, and items 2, 6 and 8 partly.
* One small addition needed: `NodeRecord` has no `port` field and
  `OpenSSHClient` emits no `-p`, which Linux CI does not need and a developer on
  macOS does.

**And what not to build.** Not the gRPC agent, and not the §8 report-only agent
either. Every property they were being bought for has now been measured over the
transport we already have: the per-call floor (0.005 forks, 0.55 ms, 6 ms round
trip), the positive heartbeat (1–2 s), the push stream (below background noise),
and CI multi-node (96 lines). What remains genuinely theirs is a typed payload
guaranteed by a compiled contract rather than by a line format, and it costs a
binary on every node, a version-skew policy, a CA, an enrollment flow, and a
failure domain — agent down, node up — whose only remedy is the SSH we would
still be running. Revisit when a measurement says SSH's ten-session ceiling or
its liveness actually bit, on two machines. Nothing here is that measurement.
