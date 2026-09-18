/** One section of the settings form.
 *
 * A rule above it, a heading, an optional line saying what the group is for,
 * and the fields under it. The brief reserves cards for top-level
 * destinations and panels: a form is read one field at a time, in order, and
 * a box drawn around every group of three fields makes each group look like a
 * separate decision that has to be made now. A hairline says the same thing
 * with less furniture — and it is what separates every other section in the
 * product.
 */

import { cn } from "@/lib/utils";

export interface SettingsSectionProps {
  title: string;
  /** One line under the heading. Two sentences at most. */
  hint?: React.ReactNode;
  /** Right of the heading — a refresh, an add. */
  actions?: React.ReactNode;
  /** The first section on a tab has nothing above it to be ruled off from. */
  first?: boolean;
  children: React.ReactNode;
  className?: string;
}

export default function SettingsSection({
  title,
  hint,
  actions,
  first,
  children,
  className,
}: SettingsSectionProps) {
  return (
    <section className={cn(first ? "pt-1" : "mt-10 border-t border-line pt-12", className)}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-[17px] font-semibold tracking-[-0.02em]">{title}</h3>
          {hint && <p className="mt-1.5 max-w-[62ch] text-[13px] leading-snug text-muted">{hint}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      <div className="mt-6 space-y-6">{children}</div>
    </section>
  );
}
