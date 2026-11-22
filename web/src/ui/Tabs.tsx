/** The segmented control: a compact sub-nav, one per page at most.
 *
 * Four `border-b-2` tab bars were in the tree, each with its own active
 * colour, and none of them answered an arrow key: `role="tablist"` promises a
 * keyboard user that Left and Right move between tabs, and a bar that declares
 * the role without implementing it is worse than one that declares nothing.
 *
 * The hub's shape: a pill per tab, `7px 12px`, 13px/500, muted until it is the
 * one you are on. Horizontal scroll rather than a wrap, so a narrow screen
 * keeps the bar one line high and the tabs stay in their order.
 */

import { useRef } from "react";
import { cn } from "@/lib/utils";

export interface TabItem {
  id: string;
  label: string;
  /** Shown as a quiet number after the label. `0` renders; `undefined` does not. */
  count?: number;
}

export interface TabsProps {
  tabs: readonly TabItem[];
  value: string;
  onChange: (id: string) => void;
  /** Names the bar for a screen reader. */
  label?: string;
  className?: string;
  /**
   * `filter` (the default) narrows a list: a segmented control the height of
   * the search box beside it, so a toolbar is one even row. `sections` switches
   * between parts of one page — a run's Overview, Logs, Events — and is drawn
   * as underlined tabs on the page's rule, the way a document has sections.
   */
  variant?: "filter" | "sections";
}

export function Tabs({ tabs, value, onChange, label, className, variant = "filter" }: TabsProps) {
  const sections = variant === "sections";
  const rowRef = useRef<HTMLDivElement>(null);

  /** Left/Right/Home/End move the selection, which is what `tablist` promises.
   *  Selection follows focus — every tab here swaps a panel that is already
   *  rendered, so there is nothing to load by arrowing past one. */
  const onKeyDown = (e: React.KeyboardEvent) => {
    const at = tabs.findIndex((t) => t.id === value);
    if (at < 0) return;
    let next = at;
    if (e.key === "ArrowRight") next = (at + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (at - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    else return;
    e.preventDefault();
    onChange(tabs[next].id);
    const el = rowRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next];
    el?.focus();
  };

  return (
    <div
      ref={rowRef}
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn(
        sections
          ? "flex max-w-full items-end gap-5 overflow-x-auto border-b border-line"
          : "inline-flex h-9 max-w-full items-stretch gap-0.5 overflow-x-auto rounded-sm border border-line bg-bg2 p-[3px]",
        className,
      )}
    >
      {tabs.map((tab) => {
        const active = tab.id === value;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(tab.id)}
            className={cn(
              "inline-flex items-center gap-1.5 whitespace-nowrap text-[13px] font-medium transition-colors duration-150",
              sections
                ? cn(
                    "-mb-px border-b-2 px-0.5 pb-2 pt-1",
                    active ? "border-blue text-text" : "border-transparent text-muted hover:text-text",
                  )
                : cn(
                    "rounded-[4px] px-3",
                    active ? "bg-surface text-text shadow-[0_0_0_1px_var(--line)]" : "text-muted hover:text-text",
                  ),
            )}
          >
            {tab.label}
            {tab.count !== undefined && (
              <>
                {/* A real space, not the flex gap: the accessible name is the
                    concatenation of the text nodes, and without this a screen
                    reader (and every by-name selector) reads "Recipes(1)". The
                    count is read as "(1)" and drawn as a badge. */}
                {" "}
                <span className="sr-only">({tab.count})</span>
                <span
                  aria-hidden="true"
                  className={cn(
                    "min-w-[18px] rounded-full px-1.5 text-center text-[11px] leading-[18px] tabular-nums",
                    active ? "bg-blue/15 text-blue2" : "bg-[color-mix(in_srgb,var(--text)_8%,transparent)] text-muted",
                  )}
                >
                  {tab.count}
                </span>
              </>
            )}
          </button>
        );
      })}
    </div>
  );
}

export default Tabs;
