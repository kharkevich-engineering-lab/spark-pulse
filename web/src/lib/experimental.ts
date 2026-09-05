/** What is unproven about multi-node, in one place — and in two kinds.
 *
 * Multi-node is implemented, not verified. Every rank is rendered, started
 * worker-first, torn down head-first and accounted for, and all of that is
 * exercised end to end — in simulation. It has never been run on two machines,
 * because there is only one DGX Spark.
 *
 * **"Unproven" and "unknown" are different states and the operator sees which
 * is which.** Two lists, because they carry very different risk:
 *
 * - `MULTI_NODE_SPECIFIED` — a source establishes what the behaviour should
 *   be (NVIDIA's DGX Spark playbooks, NCCL's own documentation or source,
 *   vLLM's, SGLang's, or `eugr/spark-vllm-docker`, which is the reference we
 *   match). We implemented what the source says. Hardware would confirm it
 *   works, not discover what it should be.
 * - `MULTI_NODE_UNSPECIFIED` — nobody documents it. A measurement is the only
 *   way to know, and until one exists the setting is a considered guess that
 *   follows the reference.
 *
 * The evidence behind every line is in `docs/upstream-cluster-parity.md`,
 * which cites each one by file and line or by URL. Keep the two in step: an
 * item is struck from here when it has been observed on hardware, and never
 * before. When both lists are empty, `cluster_experimental` flips to false in
 * config and every badge and banner disappears without a code change.
 */

export const MULTI_NODE_TITLE = "Multi-node is implemented but unverified";

export const MULTI_NODE_REASON =
  "Only one DGX Spark exists, so nothing below has ever run on two machines. " +
  "The rendering, the start and teardown ordering, the refusals and the " +
  "orphan bookkeeping are exercised in simulation; none of the following has " +
  "been observed. The first group is settled on paper — a published source " +
  "says what it should do and that is what we do — and hardware would only " +
  "confirm it. The second group is documented nowhere, so hardware is the " +
  "only way to find out at all:";

/** Documented somewhere, implemented to that documentation, never yet run. */
export const MULTI_NODE_SPECIFIED: readonly string[] = [
  "Whether the rendezvous forms across machines, for either engine. The flags are vLLM's own since 0.11.1 and SGLang's since 0.5, and at one node vLLM provably never reads them.",
  "Interface pinning against real per-role names. The names now come from each node's own registry record, read out of ibdev2netdev the way NVIDIA's guidance describes, and the pre-flight checks each one against that node's /sys.",
  "Whether naming both RoCE devices of a QSFP port reaches the fabric's full bandwidth. NVIDIA measures 92.6 + 97.3 Gbps across the pair with perftest, but publishes no NCCL_IB_HCA value for DGX Spark at all.",
  "The switchless ring's two NVIDIA-published NCCL settings behaving as documented: subnet-aware routing on, and no external net plugin.",
  "How an unreachable peer behaves over a real SSH transport — a half-open connection or a node answering slowly, rather than the clean refusal simulation raises.",
  "Anything at three or four nodes. Three is NVIDIA's switchless ring, four needs a QSFP switch, and neither has been cabled here.",
];

/** Documented by nobody. Hardware would discover the answer, not confirm it. */
export const MULTI_NODE_UNSPECIFIED: readonly string[] = [
  "Whether NCCL_IB_MERGE_NICS=0 helps or costs on this fabric. The reference sets it for a mesh; NVIDIA sets it nowhere and uses subnet-aware routing instead, which NCCL's source shows solving the same problem more finely.",
  "Whether starting workers before rank zero matters at all. Neither vLLM nor SGLang documents a required start order; SGLang's own example starts every rank at once. We follow the reference and treat it as our policy.",
  "Whether a three-node ring really sustains only half the bandwidth per pair. One community source says so; NVIDIA publishes no ring bandwidth figure and its wording implies the opposite.",
  "Whether SGLang needs --enable-dp-attention across nodes. The error it is said to work around appears nowhere in SGLang's source or issues, and SGLang's own verified two-Spark recipe omits the flag.",
  "Whether NCCL_IGNORE_CPU_AFFINITY=1 is the right direction on GB10. The variable is documented; no source explains why this hardware wants it, and the setting discards the launcher's CPU mask rather than the GPU's.",
];

/** Both lists, in the order the banner renders them, each labelled by kind.
 *
 * The label is part of the sentence rather than a separate column because the
 * banner renders a flat list, and a risk whose kind is a column heading three
 * items away is a risk read without its kind.
 */
export const MULTI_NODE_UNPROVEN: readonly string[] = [
  ...MULTI_NODE_SPECIFIED.map((item) => `Specified, unconfirmed — ${lower(item)}`),
  ...MULTI_NODE_UNSPECIFIED.map((item) => `Documented nowhere — ${lower(item)}`),
];

/** Lowercase the first letter, so the label reads into the sentence. */
function lower(item: string): string {
  return item.charAt(0).toLowerCase() + item.slice(1);
}

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
