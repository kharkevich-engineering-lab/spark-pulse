/** Nothing here yet, said once.
 *
 * An icon, one line, an optional hint, and an optional way out. Not a dashed
 * box with a paragraph in it: the operator is reading this because a list is
 * empty, and the useful information is what to do about it.
 */

import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

export interface EmptyStateProps {
  icon?: LucideIcon;
  /** One sentence. */
  children: React.ReactNode;
  /** A second, quieter line — where the thing comes from, or why none exist. */
  hint?: React.ReactNode;
  /** A `Button`, usually. */
  action?: React.ReactNode;
  className?: string;
}

export function EmptyState({ icon: Icon, children, hint, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center text-center gap-2 py-12 px-6",
        className,
      )}
    >
      {Icon && <Icon size={24} className="text-muted" aria-hidden="true" />}
      <p className="text-[15px] text-text">{children}</p>
      {hint && <p className="text-[13px] text-muted max-w-md">{hint}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export default EmptyState;
