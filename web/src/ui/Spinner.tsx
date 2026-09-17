/** The one spinner.
 *
 * Loading had five spellings — `Loader2` at 16, 18, 20, 24 and 32 px, some
 * `text-blue2`, some inheriting. A reader learns a spinner once; five of
 * them is five things to learn.
 */

import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

const SIZES = { sm: 14, md: 18, lg: 32 } as const;

export interface SpinnerProps {
  size?: keyof typeof SIZES;
  className?: string;
  /** What is being waited for. Given, the spinner is announced; withheld, it
   *  is hidden from the accessibility tree — a decoration beside a label that
   *  already says "Saving…" is noise. */
  label?: string;
}

export function Spinner({ size = "md", className, label }: SpinnerProps) {
  return (
    <Loader2
      size={SIZES[size]}
      role={label ? "status" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={cn("animate-spin shrink-0", className)}
    />
  );
}

export default Spinner;
