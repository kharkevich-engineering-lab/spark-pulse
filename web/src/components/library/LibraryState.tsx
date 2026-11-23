/** One vocabulary for what this control plane holds of a library item.
 *
 * Engines, models, recipes and mods are the same kind of thing: a source
 * offers them, the catalogue lists what the sources offer, and some of it is
 * on the nodes. Each page used to say that in its own words — *Pulled* and
 * *Not pulled*, *verified* and *partial*, *Installed* and *Not installed* —
 * with its own filters in its own order, so an operator learned four
 * vocabularies for one idea. This is the one: the same chip, the same filters
 * in the same order, on every Library page.
 */

import type { ReactNode } from "react";
import type { Translator } from "@/lib/i18n";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { TONE_TEXT, type StatusTone, type TabItem } from "@/ui";

/** Where an item stands. */
export type LibraryState =
  /** On the nodes, complete. */
  | "on_nodes"
  /** On some nodes, or only part of it. */
  | "partial"
  /** Being downloaded or pulled now. */
  | "getting"
  /** A source offers it; nothing is held. */
  | "available"
  /** Held, and a source offers a newer one. */
  | "update"
  /** Held, and edited or written here — no source has the same thing. */
  | "local"
  /** Held, and no source offers it any more. */
  | "removed"
  /** A node could not be asked. Unknown is not absent. */
  | "unknown";

const TONE: Record<LibraryState, StatusTone> = {
  on_nodes: "good",
  partial: "warn",
  getting: "warn",
  available: "muted",
  update: "warn",
  local: "good",
  removed: "muted",
  unknown: "muted",
};

/** What "held" is called for a kind. Engines and models are held *on nodes*;
 *  a recipe or a mod is held by the control plane, where it is *installed*. */
export type HeldAs = "on_nodes" | "installed";

const heldKey = (state: LibraryState, heldAs: HeldAs) =>
  state === "on_nodes" && heldAs === "installed" ? "installed" : state;

export interface LibraryStateChipProps {
  state: LibraryState;
  heldAs?: HeldAs;
  /** A few words after the label: "2 of 3 nodes", "45%". */
  detail?: ReactNode;
  /** Why, on hover. */
  title?: string;
  className?: string;
}

export function LibraryStateChip({
  state,
  heldAs = "on_nodes",
  detail,
  title,
  className,
}: LibraryStateChipProps) {
  const { t } = useI18n();
  const tone = TONE[state];
  return (
    <span
      title={title}
      data-testid={`library-state-${state}`}
      className={cn("inline-flex items-center gap-1.5 text-[13px] font-medium", TONE_TEXT[tone], className)}
    >
      <span
        aria-hidden="true"
        className={cn(
          "w-[7px] h-[7px] rounded-full shrink-0",
          state === "available" ? "border border-current" : "bg-current",
          state === "getting" && "animate-pulse",
        )}
      />
      {t(`library.state.${heldKey(state, heldAs)}`)}
      {detail != null && detail !== "" && <span className="font-normal text-muted">· {detail}</span>}
    </span>
  );
}

/** The filters every Library page offers, in this order. */
export type LibraryFilter = "all" | "on_nodes" | "available" | "updates";

/** Whether an item in ``state`` is shown under ``filter``. "On nodes" is
 *  anything held, whole or not; "available" is what a source offers that is
 *  not held. */
export function matchesFilter(state: LibraryState, filter: LibraryFilter): boolean {
  switch (filter) {
    case "all":
      return true;
    case "on_nodes":
      return state !== "available";
    case "available":
      return state === "available";
    case "updates":
      return state === "update";
  }
}

/** The filter bar's tabs with their counts. A filter that cannot apply to a
 *  kind — models have no browsable catalogue, mods no updates — is left out
 *  by naming the ones that do. */
export function libraryFilterTabs(
  t: Translator["t"],
  states: readonly LibraryState[],
  offered: readonly LibraryFilter[] = ["all", "on_nodes", "available", "updates"],
  heldAs: HeldAs = "on_nodes",
): TabItem[] {
  return offered.map((id) => ({
    id,
    label: t(`library.filter.${id === "on_nodes" && heldAs === "installed" ? "installed" : id}`),
    count: states.filter((s) => matchesFilter(s, id)).length,
  }));
}
