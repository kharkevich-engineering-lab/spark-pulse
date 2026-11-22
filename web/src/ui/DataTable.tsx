/** The one table: every list of things in the console is drawn with these.
 *
 * Runs, recipes, nodes, models, caches, engines and registries had three
 * header styles, three row heights and actions that wrapped onto two lines.
 * These pieces fix one answer for each: a small uppercase header, 40px rows,
 * numbers right-aligned in tabular figures, and an actions cell that never
 * wraps. They are thin on purpose — a cell is still a `<td>` the page fills.
 */

import { cn } from "@/lib/utils";

export function Table({
  className,
  children,
  ...rest
}: React.TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="relative min-w-0 overflow-x-auto">
      <table className={cn("w-full border-collapse text-[13px]", className)} {...rest}>
        {children}
      </table>
    </div>
  );
}

export function THead({ children }: { children: React.ReactNode }) {
  return (
    <thead>
      <tr className="border-b border-line">{children}</tr>
    </thead>
  );
}

export interface ThProps extends React.ThHTMLAttributes<HTMLTableCellElement> {
  numeric?: boolean;
  /** An actions column: named for a screen reader, drawn empty. */
  srOnly?: boolean;
}

export function Th({ numeric, srOnly, className, children, ...rest }: ThProps) {
  return (
    <th
      className={cn(
        "whitespace-nowrap px-3 py-2 text-left text-[10.5px] font-semibold uppercase tracking-[0.08em] text-muted",
        numeric && "text-right",
        className,
      )}
      {...rest}
    >
      {srOnly ? <span className="sr-only">{children}</span> : children}
    </th>
  );
}

export interface TrProps extends React.HTMLAttributes<HTMLTableRowElement> {
  /** The row is open in the side panel. */
  selected?: boolean;
  /** The whole row opens something. */
  interactive?: boolean;
  muted?: boolean;
}

export function Tr({ selected, interactive, muted, className, children, ...rest }: TrProps) {
  return (
    <tr
      aria-selected={interactive ? Boolean(selected) : undefined}
      className={cn(
        "border-b border-line last:border-b-0 transition-colors",
        interactive && "cursor-pointer",
        selected
          ? "bg-blue/10"
          : interactive && "hover:bg-[color-mix(in_srgb,var(--text)_4%,transparent)]",
        muted && "opacity-60",
        className,
      )}
      {...rest}
    >
      {children}
    </tr>
  );
}

export interface TdProps extends React.TdHTMLAttributes<HTMLTableCellElement> {
  numeric?: boolean;
  mono?: boolean;
  muted?: boolean;
  /** Truncate to one line; the full text belongs in `title`. */
  truncate?: boolean;
  /** The actions cell: right-aligned, never wraps, and a click on it does not
   *  also open the row. */
  actions?: boolean;
}

export function Td({ numeric, mono, muted, truncate, actions, className, children, onClick, ...rest }: TdProps) {
  return (
    <td
      onClick={actions ? (e) => { e.stopPropagation(); onClick?.(e); } : onClick}
      className={cn(
        "px-3 py-2 align-middle",
        numeric && "text-right font-mono tabular-nums whitespace-nowrap",
        mono && "font-mono text-[12.5px]",
        muted && "text-muted",
        truncate && "max-w-0 truncate",
        actions && "w-px whitespace-nowrap px-2 py-1 text-right",
        className,
      )}
      {...rest}
    >
      {actions ? <div className="inline-flex items-center justify-end gap-0.5">{children}</div> : children}
    </td>
  );
}

/** A row that says the list is empty, spanning the table. */
export function EmptyRow({ colSpan, children }: { colSpan: number; children: React.ReactNode }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-8 text-center text-[13px] text-muted">
        {children}
      </td>
    </tr>
  );
}

/** What an expanded row says, as a full-width strip under it.
 *
 * For things with a few lines more to say than a row holds — a model's copies,
 * an engine's ports. Anything longer has a page of its own; this stays short
 * enough that opening it never makes the list a scroll of its own.
 */
export function DetailRow({ colSpan, children, "data-testid": testId }: { colSpan: number; children: React.ReactNode; "data-testid"?: string }) {
  return (
    <tr className="border-b border-line bg-bg2 last:border-b-0" data-testid={testId}>
      <td colSpan={colSpan} className="px-3 py-3">
        {children}
      </td>
    </tr>
  );
}
