# Design notes

Longer pieces written while the decisions were being made. They are kept as they were: each one records what was surveyed, what was measured and what was chosen, so a later reader can tell a decision from a habit.

| | |
|---|---|
| [The cluster agent plan](../cluster-agent-plan.md) | Why an agent per node, why it dials out, how identity and enrolment work, and what was surveyed first (Swarm, k3s, Nomad). |
| [Transport re-examined](../transport-reexamined.md) | The audit that found thirteen call sites querying the control node while claiming to reach a worker, and what removing the local branch cost. |
| [Rank state transport](../rank-state-transport.md) | Measurements behind the liveness timeouts — including the 28.7 ms warm inspect on a GB10 that sets the probe budget. |
| [The native runtime plan](../native-runtime-plan.md) | Replacing `run-recipe.sh` with a runtime that drives Docker directly. |
| [Health history](../health-history.md) | What can honestly be charted, what a DGX Spark's `nvidia-smi` refuses to report, and why there are no percentiles. |
| [Engine metrics](../engine-metrics.md) | The sampler, its window, and the `HealthMonitor` that was removed rather than repaired. |
| [Cluster evidence](../cluster-evidence.md) | Sources for every multi-node claim, and which of them are unverified. |
| [Upstream cluster parity](../upstream-cluster-parity.md) | Feature-by-feature comparison with `spark-vllm-docker`'s cluster path, and where the two deliberately differ. |
| [Authentication](../authentication.md) | OIDC, sessions, and what is protected when auth is off. |
| [Development](../development.md) | The dev scripts, the test layers and what each one is for. |

These are not maintained as reference material — the pages under **Reference** are. Where a design note and the code disagree, the code is right and the note records why the decision was made.
