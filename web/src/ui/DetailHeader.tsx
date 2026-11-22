/** The top of a page about one thing: where it lives, what it is, what to do.
 *
 * A run, a node: things with enough to say that a panel beside the list
 * became a second scrolling column inside a scrolling page. They get a page of
 * their own instead — the back link names the list it came from, so the way
 * back is a click and the browser's Back does the same.
 */

import { Link } from "react-router-dom";
import { ChevronLeft } from "lucide-react";
import { cn } from "@/lib/utils";

export interface DetailHeaderProps {
  /** The list this thing belongs to. */
  backTo: string;
  backLabel: string;
  title: React.ReactNode;
  /** Beside the title: a status badge. */
  badge?: React.ReactNode;
  /** Under the title: an address, an endpoint. One line. */
  subtitle?: React.ReactNode;
  /** Buttons, right-aligned. */
  actions?: React.ReactNode;
  className?: string;
}

export function DetailHeader({ backTo, backLabel, title, badge, subtitle, actions, className }: DetailHeaderProps) {
  return (
    <div className={cn("mb-4 border-b border-line pb-3", className)}>
      <Link
        to={backTo}
        className="mb-2 inline-flex items-center gap-1 text-[12.5px] text-muted hover:text-text"
      >
        <ChevronLeft size={14} aria-hidden="true" />
        {backLabel}
      </Link>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
            <h1 className="min-w-0 break-words text-[20px] font-bold leading-tight tracking-[-0.02em]">{title}</h1>
            {badge}
          </div>
          {subtitle && <div className="mt-1 min-w-0 text-[13px] text-muted">{subtitle}</div>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
    </div>
  );
}

export default DetailHeader;
