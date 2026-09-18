/** The pill sub-nav.
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
}

export function Tabs({ tabs, value, onChange, label, className }: TabsProps) {
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
      className={cn("flex items-center gap-2 overflow-x-auto", className)}
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
              "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border",
              "px-3 py-[7px] text-[13px] font-medium transition-colors duration-200",
              active
                ? "border-line-strong text-text"
                : "border-line text-muted hover:text-text",
            )}
          >
            {tab.label}
            {tab.count !== undefined && (
              <>
                {/* A real space, not the flex gap: the accessible name is the
                    concatenation of the text nodes, and without this a screen
                    reader (and every by-name selector) reads "Recipes(1)". */}
                {" "}
                <span className="text-muted tabular-nums">({tab.count})</span>
              </>
            )}
          </button>
        );
      })}
    </div>
  );
}

export default Tabs;
