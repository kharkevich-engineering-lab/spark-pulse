/** What is unproven about multi-node, in one place.
 *
 * Multi-node is implemented, not verified. Every rank is rendered, started
 * worker-first, torn down head-first and accounted for, and all of that is
 * exercised end to end — in simulation. It has never been run on two machines,
 * because there is only one DGX Spark.
 *
 * The list below is the operator-facing copy of "What stays unproven until a
 * second Spark exists" in `docs/cluster-agent-plan.md` section 7. Keep the two
 * in step: an item is struck from here when it has been observed on hardware,
 * and never before. When the list is empty, `cluster_experimental` flips to
 * false in config and every badge and banner disappears without a code change.
 */

export const MULTI_NODE_TITLE = "Multi-node is implemented but unverified";

export const MULTI_NODE_REASON =
  "Only one DGX Spark exists, so nothing below has ever run on two machines. " +
  "The rendering, the start and teardown ordering, the refusals and the " +
  "orphan bookkeeping are exercised in simulation; none of the following has " +
  "been observed:";

export const MULTI_NODE_UNPROVEN: readonly string[] = [
  "Whether the rendezvous forms across machines, for either engine.",
  "Which transport NCCL selects over the real fabric, and whether the twin-adapter configuration reaches NVIDIA's throughput threshold.",
  "Interface pinning against real per-role names, including NVIDIA's rule that the two devices of one port sit on different subnets.",
  "Whether starting workers before rank zero really avoids the ten-minute collective timeout, and whether the startup gates are set right.",
  "How an unreachable peer behaves over a real SSH transport — a half-open connection or a node answering slowly, rather than the clean refusal simulation raises.",
  "Anything at three or four nodes, where the ring configuration differs and aggregate bandwidth roughly halves. Above four, NVIDIA publishes no guidance and the plan refuses outright.",
];

/** One line for a tooltip, where a list does not fit. */
export const MULTI_NODE_BADGE_TITLE =
  "Multi-node is implemented but has never been run on two machines: no second DGX Spark exists to verify it";

/** How many machines a deployment occupies. Absent or 0 means this one. */
export function nodeCount(deployment: {
  node_count?: number | null;
  nodes?: string[] | null;
}): number {
  return deployment.node_count || deployment.nodes?.length || 1;
}
