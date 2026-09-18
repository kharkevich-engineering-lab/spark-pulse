/** An error from an action: one 13px red line under the control that failed.
 *
 * The hub's `.status.error`. Eight pages carried their own copy of a padded,
 * bordered, icon-bearing banner, which puts the failure a long way from the
 * button that caused it and makes a one-line message look like an outage.
 */

import { cn } from "@/lib/utils";

export interface ErrorLineProps {
  /** Nothing renders when this is absent, so a caller can pass its error
   *  state straight in rather than guarding at every site. */
  children?: React.ReactNode;
  className?: string;
  /** So a `Field` can point the control's `aria-describedby` at it. */
  id?: string;
  /** For a caller whose tests already name this line. */
  "data-testid"?: string;
}

export function ErrorLine({ children, className, id, ...rest }: ErrorLineProps) {
  if (!children) return null;
  return (
    <p
      id={id}
      role="alert"
      className={cn("text-[13px] leading-snug text-bad", className)}
      {...rest}
    >
      {children}
    </p>
  );
}

export default ErrorLine;
