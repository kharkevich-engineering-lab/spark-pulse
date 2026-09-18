/** An inline literal — a path, a port, a command, an image reference.
 *
 * Defined three times before this, in three sizes, and only Settings' copy
 * could break a long path across lines.
 */

import { cn } from "@/lib/utils";

export interface CodeProps {
  children: React.ReactNode;
  className?: string;
}

export function Code({ children, className }: CodeProps) {
  return (
    <code
      className={cn(
        "px-1.5 py-0.5 rounded-sm bg-bg border border-line font-mono text-[12.5px] break-all",
        className,
      )}
    >
      {children}
    </code>
  );
}

export default Code;
