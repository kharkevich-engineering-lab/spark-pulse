/** One section of the settings form: a bordered surface, a heading, and its
 *  settings as rows.
 *
 * A setting is a row, not a stack: what it is and what it means on the left,
 * the control on the right. Stacked label / input / help made every section
 * tall and narrow and left the right half of the page empty; as rows, the form
 * takes the width it is given and a section is as tall as it has settings.
 * Every direct child of a section's body is a row, divided by a hairline, so
 * a block that is not a `SettingRow` (an editor, a list) lines up with them.
 */

import { useId } from "react";
import { cn } from "@/lib/utils";
import { ErrorLine } from "@/ui";
import Brief from "./Brief";

export interface SettingsSectionProps {
  title: string;
  /** One line under the heading; the first sentence shows, the rest is a tip. */
  hint?: React.ReactNode;
  /** Right of the heading — a refresh, an add. */
  actions?: React.ReactNode;
  /** The first section on a tab has no gap above it. */
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
    <section className={cn("rounded-md border border-line bg-surface", !first && "mt-4", className)}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <h3 className="text-[14px] font-semibold">{title}</h3>
          {hint && (
            <p className="mt-0.5 text-[12.5px] leading-snug text-muted">
              {typeof hint === "string" ? <Brief text={hint} /> : hint}
            </p>
          )}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      <div className="px-4 [&>*]:py-3.5 [&>*+*]:border-t [&>*+*]:border-line">{children}</div>
    </section>
  );
}

export interface SettingRowControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

export interface SettingRowProps {
  label: React.ReactNode;
  /** One line under the label saying what the value means. */
  hint?: React.ReactNode;
  error?: React.ReactNode;
  /** The control. A function receives the id and aria wiring to spread, as
   *  `Field` does, so the label names the control. */
  children: React.ReactNode | ((props: SettingRowControlProps) => React.ReactNode);
  /** The control is a switch or a value: keep it at the row's right edge. */
  end?: boolean;
  className?: string;
}

/** Label and help on the left, the control on the right; stacked under 760px. */
export function SettingRow({ label, hint, error, children, end, className }: SettingRowProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ");
  const control: SettingRowControlProps = {
    id,
    "aria-describedby": describedBy || undefined,
    "aria-invalid": error ? true : undefined,
  };

  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-x-8 gap-y-2 min-[760px]:grid-cols-[minmax(0,45fr)_minmax(0,55fr)] min-[760px]:items-start",
        className,
      )}
    >
      <div className="min-w-0">
        <label htmlFor={id} className="block text-[13.5px] font-medium text-text">
          {label}
        </label>
        {hint && (
          <p id={hintId} className="mt-0.5 text-[12.5px] leading-snug text-muted">
            {hint}
          </p>
        )}
      </div>
      <div className={cn("min-w-0", end && "min-[760px]:flex min-[760px]:justify-end")}>
        {typeof children === "function" ? children(control) : children}
        <ErrorLine id={errorId} className="mt-1">
          {error}
        </ErrorLine>
      </div>
    </div>
  );
}
