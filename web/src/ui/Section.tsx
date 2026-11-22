/** A section of a page: one bordered surface, with a quiet header row.
 *
 * Every page is a header, perhaps a toolbar, and one or more of these. A
 * section's title is 14px — never larger than the page title above it — and
 * what explains it is one line at most; anything longer goes behind an
 * `InfoTip`. `flush` drops the padding so a table can run edge to edge.
 */

import { cn } from "@/lib/utils";

export interface SectionProps {
  title?: React.ReactNode;
  /** A count or a figure after the title: "9 models", "755 GB". */
  meta?: React.ReactNode;
  /** One line under the title. */
  description?: React.ReactNode;
  /** Right of the title: buttons, a filter. */
  actions?: React.ReactNode;
  /** No padding around the body, for a table or a list with its own rows. */
  flush?: boolean;
  className?: string;
  bodyClassName?: string;
  children?: React.ReactNode;
  "data-testid"?: string;
  id?: string;
}

export function Section({
  title,
  meta,
  description,
  actions,
  flush = false,
  className,
  bodyClassName,
  children,
  id,
  ...rest
}: SectionProps) {
  const hasHeader = Boolean(title || actions || description);
  return (
    <section
      id={id}
      data-testid={rest["data-testid"]}
      className={cn("min-w-0 overflow-hidden rounded-md border border-line bg-surface", className)}
    >
      {hasHeader && (
        <header className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line px-4 py-2.5">
          <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2.5 gap-y-0.5">
            {title && <h2 className="text-[14px]">{title}</h2>}
            {meta && <span className="text-[12.5px] text-muted tabular-nums">{meta}</span>}
            {description && <p className="w-full text-[12.5px] text-muted">{description}</p>}
          </div>
          {actions && <div className="flex flex-wrap items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={cn(!flush && "p-4", bodyClassName)}>{children}</div>
    </section>
  );
}

export default Section;
