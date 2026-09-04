# Cluster agent plan

Status: proposal, 2026-09-04. Supersedes phase 4 of `native-runtime-plan.md`.

Research basis: four parallel studies of node enrollment, agent transport and
identity, cluster state and failure semantics, and artifact distribution,
against the primary sources of k3s, k0s, Docker Swarm, SwarmKit, Nomad,
Kubernetes, Salt, Tailscale, Talos, Consul, Ray, Slurm, Volcano, Kueue,
JobSet, torchrun, the OCI distribution spec and HuggingFace Hub. Facts here
are cited in those studies. Measurements marked as such were taken this
session, either on the DGX Spark at `192.168.29.60` or against live registries.

## 1. What is being decided

The proposal is to install a per-node agent, enroll it through an interactive
bootstrap that generates or accepts an SSH key, and talk to it over gRPC with
mutual TLS. The control plane keeps the state machine and the credentials.
Artifact distribution becomes a choice between the control node fetching once
and each node downloading directly.

That is the right destination. Three findings should change the order in which
we get there.

## 2. Findings that change the shape

### 2.1 A large part of the motivation is available for two lines of code

Our SSH client passes only `BatchMode=yes`. It sets no `ControlMaster` and no
`ControlPersist`, so every single docker verb pays a fresh TCP and SSH
handshake. Turning on connection multiplexing removes most of the per-operation
latency that motivates the agent, and costs almost nothing.

What multiplexing does **not** give us, and what only an agent can:

- a continuous liveness signal rather than a poll
- a structural distinction between "the node is unreachable" and "the command failed", instead of parsing stderr
- streaming with backpressure and cancellation for logs and pull progress

Those three are real and worth building for. Connection reuse is not one of
them. We should turn multiplexing on first, measure, and let the measurement
tell us how much of the agent's value is liveness rather than speed.

### 2.2 Four things we already shipped are broken, and multi-node makes them dangerous

None of these have been exercised, because multi-node has never run.

**The deployment state file is not crash-safe.** `_save` writes directly to the
target path with no temp-file-and-rename, and `_load` turns any parse or IO
error into an empty list. A crash or a full disk mid-write truncates the file
permanently, and the control plane then believes there are zero deployments.
Our own secrets code already does atomic replace correctly, though without the
`fsync` that would make it survive power loss.

This is the exact input condition behind Nomad issue 18267, where servers
instructed clients to stop and garbage-collect every allocation they were
running. Today our blast radius is small because reconciliation rebuilds from
container labels. Give an agent a rule that says "absent from the desired set
means stop it" and the same truncation tears down every job on every node.

**Image distribution destroys digest identity.** We stream
`docker save | ssh docker load`. Measured this session on a real image: the
digest changes and `RepoDigests` comes back empty. We deploy by digest-pinned
reference, so a worker seeded this way cannot resolve the image it was handed,
and a later pull re-downloads every layer instead of reusing them. It also puts
2.15 times more bytes on the wire, because the save format stores uncompressed
layers.

**Model distribution verifies nothing.** Our presence check tests that a
snapshot directory exists. HuggingFace's downloader checks file size and never
a hash, and its "already cached" test is a path existence check that follows
symlinks. A truncated transfer reads as success at every layer we have.

**The SSH strict-host-key flag is inverted.** Passing `True` emits
`StrictHostKeyChecking=no`. A test asserts the inverted behaviour, so the bug is
locked in. Two callers pass `True` plainly meaning "be strict".

There is also a fifth, structural one: most cluster operations pass an empty
host, meaning local docker. Ray worker start, every cluster health check, stop,
status and reconciliation all query the control node's own daemon. The health
check that compares NCCL settings across nodes compares the local node with
itself. Today's cluster code cannot drive a second machine at all.

### 2.3 The uplink dominates artifact distribution, not the fast link

For a 400 GB model on a 300 Mb/s uplink:

| Nodes | Each node downloads | Control fetches once, 10 GbE | Control fetches once, ConnectX-7 |
|---|---|---|---|
| 2 | 5h55m | 3h03m | 2h58m |
| 4 | 11h51m | 3h15m | 2h59m |
| 8 | 23h42m | 3h39m | 3h02m |

Fetch-once saves twenty hours at eight nodes. Moving the fan-out from 10 GbE to
the 200 Gb fabric saves thirty-seven minutes of that. Build fetch-once first and
treat the fast link as a bonus. The fabric earns its keep only when the bytes
are already local: a node joining later, a redeploy, a second job on the same
weights, where 400 GB is forty seconds instead of six minutes.

Note also that "200 Gb" is two roughly 95 Gb/s RDMA queue pairs aggregated, per
NVIDIA's own benchmark guide, not a single pipe, and it is an RDMA number rather
than a sockets one. A single SSH stream tops out near 3 Gb/s because OpenSSH's
pipeline is single-threaded, not because the cipher is slow. Measured on ARM
with crypto extensions, AES-128-GCM runs at 82 Gb/s on one core.

## 3. Recommended architecture

### 3.1 Enrollment

Reuse NVIDIA's discovery, but not their key handling. Their `discover-sparks`
browses `_ssh._tcp` over mDNS and then **copies one shared private key to every
node** to make access bidirectional. Compromise of any single Spark then yields
SSH access to all of them. That is the one thing not to carry forward: our
private key never leaves the control plane, and only the public half is pushed.

Discovery itself works. Checked on our Spark this session, which settles an open
question from the research: stock Ubuntu 24.04 ships no avahi service files at
all, so `_ssh._tcp` would find nothing, but **DGX OS does publish
`/etc/avahi/services/ssh.service`**, and `avahi-daemon`, `avahi-browse` and
`ibdev2netdev` are all present. For the switched case NVIDIA ships a netplan
profile using IPv4 link-local, so discovery needs no DHCP and no DNS.

We should still publish our own `_spark-pulse._tcp` record carrying the node id,
port and version, rather than depending on `_ssh._tcp` and then guessing whether
a responder is a Spark or an office printer.

One trap to detect rather than trip over: `spark-vllm-docker`'s networking guide
sets `link-local: []` on the fabric interfaces, which deletes the IPv6
link-local address and silently kills any `ff02::1` sweep on that link. NVIDIA's
own playbook does not set it, so both configurations will be encountered.

**Node identity is a server-minted random UUID.** Not the hostname, and
explicitly not `/etc/machine-id`. DGX Sparks are reported to ship with duplicate
machine-ids, and k0s abandoned machine-id for identity in v1.30 for exactly this
class of reason. Every system surveyed that keys identity on a name has a
documented re-enrollment failure; every system that mints a random id does not.
Keep a machine-id check purely as a diagnostic that warns about duplicates.

The flow, with every secret named and time-boxed:

1. Operator picks discovered nodes or types addresses, and gives a username.
2. Operator supplies an SSH private key, or we generate an ed25519 keypair. Only the public key is ever sent. If a key is supplied, no password is requested.
3. With no key, we ask for the node password once. We show the host key fingerprint and require confirmation before the password is sent. The password is used in-process through asyncssh, never through `sshpass`, is never persisted, and never reaches a log.
4. We add the public key, then verify passwordless SSH works.
5. The control plane mints an enrollment token scoped to that specific node name, valid ten minutes, single use. Nomad scopes tokens to a node name this way; k0sctl uses exactly a ten minute TTL.
6. We install the agent bundle and start it. The installer must detect an existing identity and either converge or refuse loudly. k0s silently ignores the token when a config already exists, which is why re-enrollment there needs a full reset.
7. The agent dials home, presents the token, and receives its certificate. It pins the CA by the SPKI over the whole trust bundle, not one certificate and not the PEM bytes, so that CA renewal does not invalidate every token.
8. The control plane invalidates the token and the installer overwrites the on-host token file.

`sudo` on our Spark requires a password, so the install step probes `sudo -n
true` first and falls back to feeding the password through the SSH channel's
stdin, never through argv or a remote `echo`. We should offer, but never
require, a narrowly scoped sudoers drop-in afterwards covering only
`systemctl start/stop/restart` for our unit.

Two details that will otherwise cost a day each. Ubuntu 24.04 defaults
`PermitRootLogin` to `prohibit-password`, so a password bootstrap as root cannot
work, and our SSH client's `user="root"` default is wrong for this hardware
where onboarding creates a normal sudo user. And certificates must be issued
with `NotBefore` backdated about five minutes, or a node whose clock is behind
rejects the certificate it was just given.

Removal is two distinct actions, never one. **Remove** wipes the node's identity
and requires re-enrollment. **Uninstall, keep identity** allows a reinstall to
rejoin. k3s's uninstall script removes its config but not its node identity,
and that asymmetry is why reinstall works but reimaging fails.

Revocation is a blacklist of node ids with expiry-based sweeping, which is what
Swarm does. It gives real revocation with no CRL and no OCSP, in a few dozen
lines.

### 3.2 Transport and identity

gRPC over `grpcio`'s asyncio API. The agent dials the control plane and holds
one long-lived bidirectional stream, so there is one inbound port on the control
plane rather than one per node, identity is authenticated once per connection,
and heartbeat liveness and command-channel liveness become the same fact.

**Identity is logical, never an address.** Each agent gets a SPIFFE-style URI
subject alternative name, `spiffe://spark-pulse/node/<uuid>`. Nomad deliberately
keeps hostnames and IPs out of agent certificates, because otherwise any service
on a host can impersonate an agent. Put the role in the certificate name and
verify it, because extended key usage will not separate a control plane from a
minion. Identity then survives DHCP, renumbering, and moving a node to the other
link.

Certificates: a ten-year CA whose key never leaves the control node, a one-year
server certificate rotated hot through the dynamic credentials fetcher, and
ninety-day agent certificates renewed at a jittered fifty to eighty percent of
remaining life over the existing authenticated channel. A node switched off past
its expiry must be able to re-obtain a certificate using its node password
rather than a fresh enrollment token, because Swarm has no such fallback and
ships a dedicated error telling you to leave the cluster and rejoin. Ninety days sits deliberately between Consul's 72 hours
and k3s's year: short enough to bound a stolen certificate, long enough that a
node switched off for a fortnight comes back on its own.

Revocation is passive plus a ledger. Most TLS implementations outside browsers
do not check CRL or OCSP at all, so the servicer checks an enrollment ledger on
every connection alongside chain validation. Reimaging is detected by carrying
`boot_id`, machine-id and a hardware fingerprint in the heartbeat: a new key for
an already-accepted uuid, or a fingerprint that no longer matches, is marked
denied and surfaced for a human decision, which is what `salt-key` does.

**Command outcomes travel as payload, never as a gRPC status.** A result that
arrives means the node was reachable and the outcome is definite. No result
means unreachable and the outcome is unknown. This is what makes
unreachable-versus-failed structural instead of a string-matching exercise, and
it sidesteps the trap that a gracefully shutting-down server also returns
`UNAVAILABLE`.

Keepalive needs care in both directions. The server's minimum ping interval must
be below the client's keepalive time, or the connection is killed with
`ENHANCE_YOUR_CALM` and the client silently doubles its interval, so detection
gets slower over hours and nobody knows why. Ten seconds client, twenty server,
five second minimum interval, pings permitted without calls on both sides.

### 3.3 State and failure

**Pull, not push.** Nomad and the kubelet pull and reconcile; Swarm pushes and
had to build sequence numbers, gap detection and a full-versus-delta distinction
to make it safe. For a control plane that restarts, a pull model makes the
restart unobservable to agents.

**SQLite in WAL mode** replaces the JSON file as the source of truth for desired
state, with a single writer thread and all access off the event loop. k3s backs
a Kubernetes API server with SQLite, so it is ample here. An unreadable state
file must refuse to start, not report an empty cluster. Container labels stay,
demoted from source of truth to recovery and adoption index, which is what the
kubelet, Swarm and Compose all do.

**Three states, not two.** Healthy, unknown, dead. Every system surveyed shipped
with two and grew a third afterwards: Nomad's `unknown`, Swarm's `ORPHANED`,
Kubernetes' `Terminating`, Volcano's `Unknown` phase. It is the design decision
most expensive to retrofit.

**Fencing is a cluster uuid plus a controller epoch**, bumped once per control
plane start and persisted by every agent, which refuses any command carrying a
lower epoch. The same counter doubles as the monotonic desired-state index that
prevents acting on stale state. Leader election is not fencing; the check has to
happen at the resource.

Timings are set by the workload, not by taste. torchrun waits 600 seconds for
rendezvous and NCCL waits 600 seconds on a collective, so anything shorter fires
while the job is still legitimately trying.

| Signal | Timing | Action |
|---|---|---|
| Missed heartbeats | 15s | Node suspect. Silent. |
| Still silent | 60s | Node unreachable. No action on the workload. |
| Agent reports a container exit | ~5s | Gang failed. Tear down. |
| Unreachable and rank 0 not serving | 120s | Gang failed. |
| Every rank must reach rendezvous | 120s gate | Fail fast, catching image and config errors in two minutes |
| Rank 0 must be ready | 720s deadline | Gang failed, release GPUs |

The asymmetry is deliberate. Evidence of death acts in seconds. Inference from
silence waits minutes. Never kill a gang because the control plane lost contact
with a node while rank 0 is still serving.

**Teardown is all-or-nothing.** Nobody in the survey restarts a single rank of a
sharded gang. Any rank failing fails the deployment; every rank of a generation
must be confirmed gone before the next generation starts; rank 0 dies first so
the rendezvous collapses cleanly instead of leaving workers in a ten-minute NCCL
timeout; and auto-restart defaults to off, as it does in JobSet and Ray Train.

A rank on an unreachable node cannot be torn down, so its GPU and ports are not
released until an agent confirms the container is gone. Every orphan bug in this
class comes from releasing a resource on inference rather than on evidence, and
with one GPU per node, two gangs sharing a slot is two processes fighting over
the same device.

Idempotency is a deterministic container name carrying deployment, rank and
generation, with Docker's atomic name reservation as the exactly-once primitive.
On a conflict: inspect, adopt if running with matching labels, purge and
recreate if present but stopped, retry with backoff otherwise.

### 3.4 Artifact distribution

**Mode A, control node fetches once, is the default.** A registry on the control
node, either a pull-through cache holding the ghcr credential and serving the
LAN anonymously, or a full local registry seeded with `skopeo copy --all
--preserve-digests`. Both were verified this session to preserve the digest
byte-identically at every hop. Nodes then pull with no credentials at all. Store
the registry base, repository and digest as three fields rather than one opaque
reference, because the host part changes per node while the digest does not.

Weights go by `rsync -a` over the fabric, copying `blobs`, `snapshots`, `refs`
and `trees` together so the relative symlinks survive, then `hf cache verify` on
each node, then an atomic rename into place, then `HF_HUB_OFFLINE=1`. Never let
path existence mean ready.

**Mode B, each node downloads directly**, must be labelled honestly in the UI as
faster to set up and worse for credentials. GitHub supports only classic
personal access tokens for its registry, so there is no way to narrow that
credential per package, and at eight nodes Mode B costs twenty extra hours on a
large model.

Its credential cost can be reduced, though not eliminated. The control node can
mint a bearer scoped to one repository, pull only, from its own token, and hand
that to nodes instead of the broad token. Measured this session: such a token
from ghcr.io was still valid after twenty-four minutes, which comfortably
outlives a 14 GB pull, so the refresh loop can be lazy rather than racing the
transfer. The catch is that GitHub documents no lifetime at all and the
distribution spec permits sixty seconds, so an implementation must re-mint and
retry on a mid-pull 401 rather than trusting the observed window. The
HuggingFace token still has to be present on every node for gated or private
weights, which is why Mode A remains the default.

Do not send model files over gRPC. Serialization dominates and Python cannot
parallelise it. gRPC instructs the agent what to fetch and streams progress; the
agent does the bulk transfer with the right tool.

## 4. Phasing

Each phase carries its own interface work, described in section 8. Shipping the
machinery without the surface is how "unknown" ends up rendered as a spinner.

**Phase A, fix what is broken.** Atomic writes with fsync for deployment state
and settings, refuse to start on an unreadable state file, the inverted SSH flag
and the test that locks it in, and the empty-host defaults so remote operations
are actually remote. Turn on SSH connection multiplexing and measure. Fill the
end-to-end suite, which currently contains no specs and reports success while
asserting nothing. None of this needs a second machine.

**Phase B, distribution without an agent.** Local registry or pull-through
cache, digest-preserving image seeding, verified model replication. This
delivers the credential-isolation goal on its own, over SSH, with no agent.
Interface: per-node replication progress, which nodes hold a verified copy, and
the two distribution modes presented by their credential trade-off rather than
by speed.

**Phase C, node registry, pre-flight, and the size-one convergence.** Persisted
nodes with address, user, key path and interfaces. Non-interactive discovery
reusing mDNS. Pre-flight that checks reachability, docker, GPU, toolkit, image
parity, model presence and free ports, and says exactly what is missing. This is
also where the six-step convergence in section 7 lands, ending with the cluster
orchestrator deleted. Interface: the Cluster page becomes a node registry with
an enrollment wizard, the deploy form gains a node count defaulting to one, and
deployments become rank-aware.

**Phase D, the agent.** Enrollment, gRPC transport, mTLS and the ledger,
packaging and upgrade, the state machine above. Gate this on the phase A
measurement: if liveness and streaming are the remaining pain, build it; if the
pain was latency, multiplexing already fixed it. Interface: three-state node
health, agent version and skew, and the removal actions distinguished.

**Phase E, multi-node bring-up.** Blocked on a second Spark. Everything before
this is verifiable on one machine, including a size-one cluster driven through
the general path. Interface: the experimental flag flips off, and the banner's
list of unproven things shrinks to nothing.

## 5. What this costs

Two deployables with a wire contract between them, and every schema change
becomes a two-sided change with a compatibility window. A CA to run, rotate and
back up, where losing the key means re-enrolling every node. An agent to package,
ship and upgrade, with a version skew policy and a rollback path. That is the
recurring cost the SSH approach does not have, and it is why phase D is gated
rather than assumed.

Two specific traps worth budgeting for. Our docker SDK is synchronous, and one
blocking call inside an asyncio handler starves every other RPC in the process,
so a slow pull looks exactly like a dead node. And gRPC does not interrupt a
running handler, so cancellation has to be plumbed explicitly or every closed log
stream leaks a `docker logs -f`.

The agent runs non-root in a dedicated group owning the docker socket. That is
not a security boundary, since docker socket access is root-equivalent, but it
means a path traversal or an unsafe extract cannot become root by accident, only
deliberately through one auditable choke point. Rootless docker does not help
here: its host networking is namespaced rather than real, which multi-node NCCL
over the direct link needs.

## 6. Decisions taken

Every question this plan opened has been settled, most of them by testing on the
Spark rather than by argument.

**python-zeroconf coexists with avahi.** Tested on the Spark with avahi-daemon
running and bound to 5353 on both address families. Our process constructed,
registered a `_spark-pulse._tcp` service, and browsed successfully, finding both
its own record and avahi's `_ssh._tcp` advertisement for the host. Independently,
`avahi-browse` saw the service our process had registered. avahi stayed active
throughout and shutdown was clean. So in-process mDNS is viable and we do not
need to shell out to `avahi-browse`, though keeping it as a cross-check for
clusters configured by NVIDIA's script is still worthwhile.

One caveat the test exposed: browsing with all interfaces announced the service
on the docker bridge and on a veth pair as well as the real network. Restrict
announcements and browsing to the interfaces `ibdev2netdev` reports up plus the
management link, or every container on the host becomes mDNS noise.

**asyncssh is the SSH library.** Spark Pulse is MIT. asyncssh offers EPL-2.0 or
GPL-2.0-or-later, and we take the EPL-2.0 option. EPL-2.0's copyleft is
file-scoped: it reaches modifications of asyncssh's own files, not code that
merely imports it, and it permits commercial use and redistribution. Declaring
it as a dependency in `pyproject.toml`, so that pip fetches it from PyPI, is not
us distributing it at all. The one thing that would change this analysis is
bundling: if we ever ship the control plane as a self-contained archive with its
dependencies inside, asyncssh travels with it and EPL-2.0's source-availability
obligation attaches to that copy. The agent bundle is unaffected, because the
agent speaks gRPC and never needs SSH. This is an engineering reading, not legal
advice; if the project ever ships a bundled commercial distribution, have it
reviewed.

**The web UI accepts an SSH password, in Proxmox's shape.** Proxmox asks for the
peer's password in its cluster-join dialog and gets two structural details right
that we copy. The shareable join blob carries only discovery and trust-pinning
data and never the credential, and the credential field is never pre-filled,
because as their UI says, for security reasons the password has to be entered
manually. Layered on that: the token installer and key paths are offered first,
the password is a disclosed fallback, it is confirmed against the host key
fingerprint before it is sent, it is used once for one node, it is modelled as a
`SecretStr`, it is never persisted or logged, and the endpoint refuses to serve
over plaintext except on loopback.

**A single-node deployment is a cluster of size one.** From now on there is one
code path, and adding a second node is configuration rather than a second
implementation. This is the largest change to the plan above and it reshapes
phase D and E; the architecture for it is being researched separately, with the
explicit goal that onboarding node two introduces no duplicate code.

## 7. Converging on a cluster of size one

Researched 2026-09-04. The decision is taken; this is how.

**We are closer than the code suggests.** A size-one cluster already runs end to
end through the native path. `Topology.is_solo` is `size <= 1`, so a one-element
node list is already solo, and planning, starting, listing, stopping and
deleting a deployment with one node in the list all work. The only thing that
refuses it is a single guard in the dispatcher that raises whenever a node list
is present. So the work is not routing solo through the cluster code. It is
growing the working native path from one container to N, and deleting the
cluster code.

**What the survey says about N=1.** Every system looked at treats one node as a
degenerate configuration rather than a special case, and the one that ever had a
distinct single-node path deleted it. Two idioms recur. Never taint, where
colocation is the default and isolation is opt-in, which is k3s, minikube and
Swarm. Or taint then untaint, which is kubeadm and kind. For hardware where the
control plane is also the only GPU, never taint is obviously right.

More useful still, none of them selects a local implementation per call. Swarm's
manager runs its own agent over a unix-socket loopback transport speaking the
identical protocol. k3s aims the ordinary remote join path at localhost. Nomad
seeds the client's server list with the local address so a colocated client goes
over loopback like any other.

**The interface boundary.** A node-bound container service, constructed for one
node, with no host argument on any method. Resolution happens once, in a
registry: the control node gets the local Docker SDK, a peer gets docker over
SSH, simulation gets the mock. The present pattern, where every call site passes
a host that defaults to empty meaning local, is a defect generator rather than a
defect, and it has already fired. Worse, the contract test that exists to catch
this hardcodes a remote address, so the local branch of the remote service has
never been exercised.

**What actually differs at N=1, and it is one thing.** vLLM's rendezvous flags
are stock upstream since 0.11.1 and at one node they are provably never read,
because vLLM derives a file-based store instead of a TCP one below two nodes. So
we can emit them at every size and stop rewriting commands for solo. SGLang
already renders uniformly, and honours its rendezvous address even at one node,
so that address should be loopback rather than the fabric IP.

The single real difference is interface pinning. `NCCL_SOCKET_IFNAME` and
`GLOO_SOCKET_IFNAME` are resolved eagerly, before any rank-count logic, with
find-or-fail semantics. Solo deployments get none of these today because the
topology carries no nodes. The moment a size-one cluster carries a real node,
they would start being emitted on a path that has never had them. That is the
one behavioural regression risk in the whole convergence, and it belongs behind
a node-count gate in one commented place.

**Sequencing.** Branch by abstraction, not strangler fig, and preparatory
refactoring rather than fix-first. Fix-first is impossible here because the
cluster code has no tests and has never run on two machines, so there is no
oracle for what fixed means. Steps one to six need no second Spark.

1. Node registry including the control node itself, populated from the existing discovery code. Nothing consumes it yet.
2. The node-bound service boundary. Make the two implementations signature-identical, bind the node at construction, delete the host argument, and extend the contract test to run every implementation against both a self node and a remote one. The empty-host bug becomes a failing test.
3. Uniform engine rendering at every size, with the interface-pinning gate. Re-verify on the Spark for both engines before going further.
4. Topology becomes total, constraints get enforced at plan time, and the capacity model stops assuming multiple GPUs per node.
5. Start becomes a loop over ranks, workers first and head last, with per-rank container names carrying a generation. At one node every loop has length one and behaviour is unchanged. Re-verify on the Spark.
6. Flip the dispatcher, then delete the cluster orchestrator, its health module, the Ray module and their mocks. This is the first step that removes capability, so it comes last.

**Where this stands, 2026-09-04.** Steps one, two and three are merged. Steps
four and five are written and are waiting on the open pre-flight branch. Step
six is done: the dispatcher no longer refuses a node list, `uses_native` no
longer reads one, and `tools/cluster.py`, `tools/cluster_health.py`,
`tools/ray.py`, `tools/cluster_models.py`, their mocks, their tests and the
whole `/api/cluster/*` router are gone, along with the `/sse/cluster` stream and
the cluster half of the health monitor.

What an operator loses with them, and what replaces it: the cluster list is
`/api/nodes`; cluster status is a deployment and its node count; start and stop
are `POST` and `DELETE /api/deployments`. Two-phase validate-and-rollback of a
cluster launch is gone outright — a native deploy has one phase, and a failed
one is torn down rather than rolled back. Ray is gone outright, and was never
implemented; `native-runtime-plan.md` appendix A keeps its specification. The
lock endpoint is gone because nothing acquires a lock any more: idempotency is
Docker's atomic name reservation, per section 3.3.

Because step six landed before step five, `native_runtime.start` still refuses a
topology larger than one — by name, saying how many ranks were asked for and how
many this build starts. That refusal is the last thing the per-rank loop
deletes.

**A CI finding worth owning.** The end-to-end job has been reporting success
while asserting nothing, because the Playwright suite contains no spec files at
all. I made that worse early in this session by adding a pass-with-no-tests flag
to get the job green. The convergence work should fill that suite, starting with
a size-one deployment through the general path.

**What stays unproven until a second Spark exists.** Whether the rendezvous
forms across machines for either engine. NCCL transport selection over the real
fabric, and whether the twin-adapter configuration reaches NVIDIA's throughput
threshold. Interface pinning against real per-role names, including NVIDIA's rule
that the two devices of one port sit on different subnets. Whether workers-first
ordering actually avoids the ten-minute collective timeout, and whether the
startup gates are set right. Failure semantics with a genuinely unreachable peer.
Anything at three nodes, where the ring configuration differs and bandwidth
roughly halves, or above four, where NVIDIA publishes no guidance at all.

**One hardware fact that changes recipe tuning.** `nvidia-smi` reports no GPU
memory on this hardware, verified: total, used and free all come back as not
available, because the GPU shares the 121 GB unified pool. Our monitoring already
degrades honestly rather than reporting zeros. But it means any capacity check
reading those fields is reading nothing, and a `gpu_memory_utilization` value
copied from an x86 recipe is untrustworthy. NVIDIA's own Spark recipes use 0.4
single-node and 0.8 to 0.9 for two nodes.

## 8. What the operator sees

The plan above is all machinery. This is the surface, and it is where most of
the design's honesty has to live, because an operator cannot read our state
machine.

**Nodes.** The Cluster page becomes a node registry rather than two free-text IP
boxes whose contents vanish on refresh. Each node shows its name, address, the
interfaces we derived rather than guessed, whether it is the control plane, its
agent version once an agent exists, and its state. Adding a node is a wizard:
pick a discovered peer or type an address, choose the installer one-liner or a
key or a password, confirm the host key fingerprint against what the dialog
shows before anything secret is sent, then watch enrollment progress. Removal is
three distinct actions and never one ambiguous button: remove and wipe identity,
uninstall but keep identity so a reinstall rejoins, and forget a node that is
already gone.

**Three states, shown as three states.** Healthy, unknown and dead must be
visually distinct everywhere they appear. Unknown is the one that matters and the
one every surveyed system had to retrofit. A node we cannot reach while its rank
is still serving is amber and says so in words: status unverified, not failed.
Never show a spinner where the honest answer is that we do not know.

**Deployments become rank-aware.** The Jobs page shows one row per deployment
and, expanded, one line per rank with its node, container, state and log tail.
When a gang fails, the failure names the rank and the cause rather than
reporting that the deployment stopped. During startup each rank shows where it
is: pulling, starting, awaiting rendezvous. The two-minute gate exists precisely
so an image that cannot be pulled surfaces in two minutes rather than twelve,
and that is worth nothing if the UI shows a blank spinner throughout.

**The deploy form gains a node count.** Default is one, which is now a cluster of
size one rather than a separate mode. Choosing more nodes picks from the
registry. The preview already shows the rendered command and image; it should
also show the per-rank breakdown and, when the image is absent, say how many
gigabytes will download first.

**Progress that reflects reality.** Image pulls and model replication both report
per-node progress with bytes and a rate, because these are hour-scale operations
on this hardware. The images view already distinguishes not pulled from a newer
digest published; model replication needs the same treatment, including which
nodes hold a verified copy.

**Diagnostics rather than mysteries.** A panel that checks and explains the
things that otherwise cost an afternoon: duplicate machine identifiers across
peers, which is a known defect on this hardware and produces confusing mDNS
symptoms; hostname churn in the mDNS log; interfaces configured without a
link-local address, which silently disables peer sweeps; and whether GPU memory
reporting is available at all, since on this hardware it is not.

**Experimental marking is temporary and flag-driven.** The cluster badge and
banner are already backed by a config flag rather than hardcoded. When a two-node
bring-up is verified on hardware, the flag flips and both disappear without a
code change. The banner should name what is unproven, not merely say
experimental, and that text should shrink as items are verified.

**Credentials, in the interface.** The two distribution modes are presented with
their real trade-off, not as a speed setting: fetch once keeps every credential
on the control node, direct download is faster to set up and puts a broad
registry token and the model-hub token on every machine. Say that in the UI, not
only in this document.

## 9. The one thing still blocked

A second DGX Spark. Everything else in this plan can be built and verified on the
one that exists.
