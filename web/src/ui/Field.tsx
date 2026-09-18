/** The form controls, and the label/hint/error frame around them.
 *
 * `inputCls` was declared twice (Settings and Engines) and written out by hand
 * a dozen more times, in two radii and two type sizes, half of them
 * monospaced because one of the two originals was. The frame is the part that
 * was never shared at all: a hint was a `<p className="text-xs">` wherever
 * somebody remembered one, and an error was usually a banner at the top of the
 * page rather than a line under the control that failed.
 *
 * `Field` wires the three together — the label points at the control, the hint
 * and the error are named by `aria-describedby`, and an error sets
 * `aria-invalid` — so a screen reader reads the failure with the field rather
 * than announcing it somewhere else on the page.
 */

import { useId } from "react";
import { cn } from "@/lib/utils";
import { ErrorLine } from "./ErrorLine";

/** Shared by input, select and textarea: `10px 12px`, 14px, on `--bg` so the
 *  control is darker than the surface it sits on. */
const CONTROL =
  "w-full px-3 py-2.5 text-[14px] rounded-sm bg-bg border border-line text-text " +
  "placeholder:text-muted transition-colors duration-200 " +
  "focus:border-blue focus:outline-none " +
  "aria-[invalid]:border-bad disabled:opacity-[0.55] disabled:cursor-not-allowed";

export type InputProps = React.InputHTMLAttributes<HTMLInputElement> & {
  /** Paths, ports and image references read better in the mono face. */
  mono?: boolean;
};

export function Input({ className, mono, ...rest }: InputProps) {
  return <input className={cn(CONTROL, mono && "font-mono text-[13px]", className)} {...rest} />;
}

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

export function Select({ className, children, ...rest }: SelectProps) {
  return (
    <select className={cn(CONTROL, className)} {...rest}>
      {children}
    </select>
  );
}

export type TextareaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement> & {
  mono?: boolean;
};

export function Textarea({ className, mono, ...rest }: TextareaProps) {
  return (
    <textarea
      className={cn(CONTROL, "resize-y", mono && "font-mono text-[13px]", className)}
      {...rest}
    />
  );
}

export interface FieldProps {
  label: string;
  /** One line under the control saying what the value means. */
  hint?: React.ReactNode;
  /** One red line under the control. Absent or empty renders nothing. */
  error?: React.ReactNode;
  /** The control. A function receives the id and aria wiring to spread. */
  children: React.ReactNode | ((props: FieldControlProps) => React.ReactNode);
  className?: string;
}

export interface FieldControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
}

export function Field({ label, hint, error, children, className }: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ");

  const control: FieldControlProps = {
    id,
    "aria-describedby": describedBy || undefined,
    "aria-invalid": error ? true : undefined,
  };

  return (
    <div className={cn("space-y-1.5", className)}>
      <label htmlFor={id} className="block text-[13px] font-medium text-text">
        {label}
      </label>
      {typeof children === "function" ? children(control) : children}
      {hint && (
        <p id={hintId} className="text-[13px] leading-snug text-muted">
          {hint}
        </p>
      )}
      <ErrorLine id={errorId} className="mt-1">
        {error}
      </ErrorLine>
    </div>
  );
}

export default Field;
