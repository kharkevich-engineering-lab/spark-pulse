/** A page's toolbar row: title, one muted line, and the page's actions.
 *
 * It used to be the hub's `.section-heading` — an 11px eyebrow over a 36px
 * extrabold title, with the description in a second column — which is the
 * right shape for a landing page and the wrong one for a tool: on a laptop it
 * pushed the first useful row a third of the way down every page. A console
 * names the page in the size of its body text and gives the screen to the
 * data, so this is one row that wraps under 900px.
 *
 * `eyebrow` is still accepted and rendered for screen readers only, so a page
 * that names its section keeps saying so without spending a line on it.
 */

import { cn } from "@/lib/utils";

export interface PageHeaderProps {
  /** The section this page is in. Read out, not drawn: the nav already shows it. */
  eyebrow?: string;
  title: string;
  /** One line. If it needs two sentences it belongs on the page, not here. */
  description?: React.ReactNode;
  /** Buttons, right-aligned. */
  actions?: React.ReactNode;
  className?: string;
}

export function PageHeader({ eyebrow, title, description, actions, className }: PageHeaderProps) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-2 mb-4 pb-3 border-b border-line",
        className,
      )}
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 gap-y-0.5">
        {eyebrow && <span className="sr-only">{eyebrow}</span>}
        <h1 className="text-[20px] font-bold tracking-[-0.02em] leading-tight">{title}</h1>
        {description && <p className="min-w-0 text-[13px] text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export default PageHeader;
