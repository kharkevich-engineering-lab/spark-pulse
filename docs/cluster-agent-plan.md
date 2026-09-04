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

Reuse what NVIDIA already ships rather than inventing discovery. Their
`discover-sparks` browses `_ssh._tcp` over mDNS and pushes SSH keys
bidirectionally. Confirmed on our Spark: `avahi-daemon` is active, it publishes
`ssh.service`, and `avahi-browse` and `ibdev2netdev` are both present. For the
switched case NVIDIA ships a netplan profile using IPv4 link-local, so
discovery works with no DHCP and no DNS.

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

`sudo` on our Spark requires a password, so the systemd install step must plan
for an interactive sudo over the same channel rather than assuming `NOPASSWD`.

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
ninety-day agent certificates renewed at thirty days remaining over the existing
authenticated channel. Ninety days sits deliberately between Consul's 72 hours
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
credential per package. Mode B puts a broad token and the HuggingFace token on
every node, which is exactly what the design is trying to avoid, and at eight
nodes it costs twenty extra hours on a large model.

Do not send model files over gRPC. Serialization dominates and Python cannot
parallelise it. gRPC instructs the agent what to fetch and streams progress; the
agent does the bulk transfer with the right tool.

## 4. Phasing

**Phase A, fix what is broken.** Atomic writes with fsync for deployment state
and settings, refuse to start on an unreadable state file, the inverted SSH flag
and the test that locks it in, and the empty-host defaults so remote operations
are actually remote. Turn on SSH connection multiplexing and measure. None of
this needs a second machine.

**Phase B, distribution without an agent.** Local registry or pull-through
cache, digest-preserving image seeding, verified model replication. This
delivers the credential-isolation goal on its own, over SSH, with no agent.

**Phase C, node registry and pre-flight.** Persisted nodes with address, user,
key path and interfaces. Non-interactive discovery reusing mDNS. Pre-flight that
checks reachability, docker, GPU, toolkit, image parity, model presence and free
ports, and says exactly what is missing.

**Phase D, the agent.** Enrollment, gRPC transport, mTLS and the ledger,
packaging and upgrade, the state machine above. Gate this on the phase A
measurement: if liveness and streaming are the remaining pain, build it; if the
pain was latency, multiplexing already fixed it.

**Phase E, multi-node bring-up.** Blocked on a second Spark. Everything before
this is verifiable on one machine, including a single-node cluster driven
through the cluster path.

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

## 6. Open questions

- Whether python-zeroconf coexists with the running avahi-daemon on port 5353, or whether we should shell out to `avahi-browse`.
- Whether to accept an SSH password in the web UI at all. The recommendation is yes, as a gated fallback behind the key and token paths, never persisted, over TLS or loopback only. The evidence is genuinely split: Uyuni ships exactly this, Portainer is deprecating theirs, Rancher has none.
- Whether a single-node cluster through the cluster path is worth building as a proving ground before hardware exists. It exercises registry, pre-flight, per-rank scripts and health, leaving only worker SSH and cross-host rendezvous unproven.
