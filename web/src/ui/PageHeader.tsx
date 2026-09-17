/** The hub's `.section-heading`, as a component.
 *
 * Eyebrow and title on the left, the one-line description and the page's
 * actions on the right, aligned to the baseline of the title. Two equal
 * columns down to 900px, one column under it — so the description never
 * becomes a narrow ribbon beside a wide title.
 *
 * Nothing calls this yet: the pages get it with the shell, in the next change.
 * It lands here so the shell is a layout change rather than a layout change
 * plus a new component.
 */

import { cn } from "@/lib/utils";

export interface PageHeaderProps {
  /** 11px uppercase accent above the title — the section this page is in. */
  eyebrow?: string;
  title: string;
  /** One line. If it needs two sentences it belongs on the page, not here. */
  description?: React.ReactNode;
  /** Buttons, right-aligned under the description. */
  actions?: React.ReactNode;
  className?: string;
}

export function PageHeader({ eyebrow, title, description, actions, className }: PageHeaderProps) {
  return (
    <div
      className={cn(
        "grid grid-cols-1 items-end gap-6 mb-8 min-[900px]:grid-cols-2 min-[900px]:gap-x-12",
        className,
      )}
    >
      <div>
        {eyebrow && (
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-brand-accent mb-2">
            {eyebrow}
          </p>
        )}
        <h1 className="text-[36px] font-extrabold tracking-[-0.03em] leading-tight max-[520px]:text-[28px]">
          {title}
        </h1>
      </div>
      {(description || actions) && (
        <div className="flex flex-col gap-3 min-[900px]:items-end">
          {description && <p className="text-[15px] text-muted">{description}</p>}
          {actions && (
            <div className="flex flex-wrap items-center gap-2 min-[900px]:justify-end">{actions}</div>
          )}
        </div>
      )}
    </div>
  );
}

export default PageHeader;
