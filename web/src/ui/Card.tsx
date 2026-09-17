/** A panel: 8px radius, one hairline, the surface colour.
 *
 * The brief reserves cards for top-level destinations and panels — sections
 * are separated by a rule, not by a box — so this is deliberately plain. No
 * shadow, and a hover lift only where the whole card is a target.
 */

import { cn } from "@/lib/utils";

export interface CardProps extends React.HTMLAttributes<HTMLDivElement> {
  /** `panel` is 24px, `destination` the 32/28 of a top-level card. */
  padding?: "panel" | "destination" | "none";
  /** Lift on hover. Only for a card the whole of which is clickable. */
  interactive?: boolean;
  children?: React.ReactNode;
}

const PADDING = {
  panel: "p-6",
  destination: "px-7 py-8",
  none: "",
} as const;

export function Card({
  padding = "panel",
  interactive = false,
  className,
  children,
  ...rest
}: CardProps) {
  return (
    <div
      className={cn(
        "rounded-md bg-surface border border-line",
        PADDING[padding],
        interactive &&
          "transition-[transform,border-color] duration-200 hover:-translate-y-0.5 hover:border-line-strong",
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}

export default Card;
