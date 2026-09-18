/** The one button.
 *
 * Three dialects of "primary" were in the tree — `bg-primary
 * hover:bg-primary-hover … text-white`, `bg-primary/10 text-blue2 border
 * border-primary/30`, and `bg-primary … text-primary-foreground
 * hover:bg-primary/90` — across roughly twenty sites, in four paddings and
 * three radii. They are one control, so this is one component.
 *
 * `loading` replaces the icon rather than sitting beside it: the button keeps
 * its width, so a row of them does not reflow the moment one is pressed, and
 * it disables at the same time because a second click on a request already in
 * flight is never what the operator meant.
 */

import { forwardRef } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Spinner } from "./Spinner";

export type ButtonVariant = "primary" | "ghost" | "danger";
export type ButtonSize = "md" | "sm";

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** A lucide component, not an element — the button decides its own size. */
  icon?: LucideIcon;
  /** Swaps the icon for a spinner and disables the button. */
  loading?: boolean;
  children?: React.ReactNode;
}

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-blue-cta text-white border-transparent hover:bg-primary-hover",
  ghost: "bg-transparent text-text border-line hover:border-line-strong",
  danger: "bg-transparent text-bad border-[#ef444466] hover:border-bad",
};

const PADDING: Record<ButtonSize, string> = {
  md: "px-[18px] py-[11px] text-[14px] gap-2",
  sm: "px-3 py-[7px] text-[13px] gap-1.5",
};

/** Everything but the padding, so the square icon button can set its own. */
const SHELL =
  "inline-flex items-center justify-center rounded-sm border font-semibold whitespace-nowrap transition-colors duration-200 disabled:opacity-[0.55] disabled:pointer-events-none";

const ICON_PX: Record<ButtonSize, number> = { md: 16, sm: 14 };

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "ghost",
    size = "md",
    icon: Icon,
    loading = false,
    disabled,
    className,
    children,
    type = "button",
    ...rest
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={cn(SHELL, VARIANTS[variant], PADDING[size], className)}
      {...rest}
    >
      {loading ? (
        <Spinner size={size === "sm" ? "sm" : "md"} />
      ) : (
        Icon && <Icon size={ICON_PX[size]} className="shrink-0" />
      )}
      {children}
    </button>
  );
});

/** A square button whose whole content is one icon.
 *
 * 40×40 at `md`, per the brief; `sm` is 32, for a control inside a table row
 * rather than beside a heading. `label` is required because there is no text
 * to read: an icon button with no accessible name is a button nobody can
 * describe.
 */
export interface IconButtonProps
  extends Omit<ButtonProps, "children" | "icon" | "size"> {
  icon: LucideIcon;
  label: string;
  size?: ButtonSize;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  function IconButton(
    { icon: Icon, label, size = "md", variant = "ghost", loading = false, disabled, className, type = "button", ...rest },
    ref,
  ) {
    return (
      <button
        ref={ref}
        type={type}
        aria-label={label}
        title={label}
        disabled={disabled || loading}
        aria-busy={loading || undefined}
        className={cn(
          SHELL,
          VARIANTS[variant],
          size === "md" ? "w-10 h-10" : "w-8 h-8",
          className,
        )}
        {...rest}
      >
        {loading ? (
          <Spinner size="sm" />
        ) : (
          <Icon size={size === "md" ? 18 : 16} />
        )}
      </button>
    );
  },
);

export default Button;
