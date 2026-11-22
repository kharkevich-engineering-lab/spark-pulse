/** The longer explanation, kept one hover or one tap away.
 *
 * A page that is read keeps one line per section; the paragraph that used to
 * sit under it lives here. Hover or focus shows it, a click pins it, Escape
 * puts it away.
 */

import { useEffect, useId, useRef, useState } from "react";
import { Info } from "lucide-react";
import { cn } from "@/lib/utils";

export interface InfoTipProps {
  /** The accessible name of the button: "About the fabric". */
  label: string;
  children: React.ReactNode;
  className?: string;
}

export function InfoTip({ label, children, className }: InfoTipProps) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  // Open towards the side with room: a tip near the right edge of the page
  // anchors right rather than spilling past it.
  const [alignRight, setAlignRight] = useState(false);
  const id = useId();
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open || !ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    setAlignRight(rect.left + 330 > window.innerWidth);
  }, [open]);

  useEffect(() => {
    if (!pinned) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setPinned(false);
        setOpen(false);
      }
    };
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) {
        setPinned(false);
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, [pinned]);

  return (
    <span
      ref={ref}
      className={cn("relative inline-flex align-middle", className)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => !pinned && setOpen(false)}
    >
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onFocus={() => setOpen(true)}
        onBlur={() => !pinned && setOpen(false)}
        onClick={() => {
          setPinned((p) => !p);
          setOpen(true);
        }}
        className="rounded p-0.5 text-muted hover:text-text"
      >
        <Info size={14} aria-hidden="true" />
      </button>
      {open && (
        <span
          id={id}
          role="tooltip"
          className={cn(alignRight ? "right-0" : "left-0", "absolute top-full z-50 mt-1 w-[320px] max-w-[80vw] rounded-sm border border-line bg-bg2 p-2.5 text-[12.5px] font-normal normal-case leading-snug tracking-normal text-text shadow-lg")}
        >
          {children}
        </span>
      )}
    </span>
  );
}

export default InfoTip;
