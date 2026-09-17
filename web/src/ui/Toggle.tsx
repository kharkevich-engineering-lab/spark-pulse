/** A switch: on or off, applied immediately.
 *
 * Four copies were in the tree, in three sizes and two colours, and two of
 * them were a `<div>` with an `onClick` — no role, no keyboard, invisible to a
 * screen reader. One size, one colour, and a real `role="switch"` button so
 * Space and Enter work without anything being written for them.
 */

import { cn } from "@/lib/utils";

export interface ToggleProps {
  on: boolean;
  onChange: (next: boolean) => void;
  /** The accessible name. Required — a switch with no name says nothing. */
  label: string;
  disabled?: boolean;
  className?: string;
  id?: string;
}

export function Toggle({ on, onChange, label, disabled, className, id }: ToggleProps) {
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className={cn(
        "relative w-11 h-6 shrink-0 rounded-full border border-line transition-colors duration-200",
        "disabled:opacity-[0.55] disabled:cursor-not-allowed",
        on ? "bg-blue-cta border-transparent" : "bg-bg2",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "absolute top-0.5 left-0.5 w-[18px] h-[18px] rounded-full transition-transform duration-200",
          on ? "translate-x-5 bg-white" : "translate-x-0 bg-muted",
        )}
      />
    </button>
  );
}

export default Toggle;
