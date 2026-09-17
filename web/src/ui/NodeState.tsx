/** Whether a node is answering, in one vocabulary.
 *
 * Six places said this in six ways: the registry had healthy/unknown/dead in
 * coloured pills, the doctor had ok/warn/broken/unknown as icons, the fabric
 * card had configured/proposed/unknown/refused, Memory had a `CloudOff` chip,
 * and Engines and Models each said "could not ask" in their own words and
 * colours. They are one question — is this machine answering, and if not, do
 * we know why — so they get one answer.
 *
 * `unknown` is the state that matters and the reason this is muted rather than
 * red: a node that could not be reached has not failed. A page that renders
 * silence as a failure teaches an operator to ignore it.
 */

import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { TONE_TEXT, type StatusTone } from "./StatusBadge";

export type NodeCondition = "ok" | "warn" | "bad" | "unknown";

const TONE: Record<NodeCondition, StatusTone> = {
  ok: "good",
  warn: "warn",
  bad: "bad",
  unknown: "muted",
};

/** The default wording, and the sentence on hover that says what it means. */
const COPY: Record<NodeCondition, { label: string; title: string }> = {
  ok: { label: "nodeState.ok", title: "nodeState.okTitle" },
  warn: { label: "nodeState.warn", title: "nodeState.warnTitle" },
  bad: { label: "nodeState.bad", title: "nodeState.badTitle" },
  unknown: { label: "nodeState.unknown", title: "nodeState.unknownTitle" },
};

export interface NodeStateProps {
  state: NodeCondition;
  /** Overrides the default word — "Configured", "Absent", "Same image". */
  label?: string;
  /** Overrides the sentence on hover; usually the node's own error. */
  title?: string;
  /** Just the dot, for a dense row where the word is already in the cell. */
  dotOnly?: boolean;
  className?: string;
}

export function NodeState({ state, label, title, dotOnly, className }: NodeStateProps) {
  const t = useT();
  const copy = COPY[state] ?? COPY.unknown;
  const text = label ?? t(copy.label);
  const hover = title ?? t(copy.title);

  return (
    <span
      title={hover}
      data-testid={`node-state-${state}`}
      className={cn(
        "inline-flex items-center gap-1.5 text-[13px] font-medium",
        TONE_TEXT[TONE[state] ?? "muted"],
        className,
      )}
    >
      <span aria-hidden="true" className="w-[7px] h-[7px] rounded-full bg-current shrink-0" />
      {dotOnly ? <span className="sr-only">{text}</span> : text}
    </span>
  );
}

export default NodeState;
