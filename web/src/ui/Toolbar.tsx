/** The row between a page's header and its content: search, filters, a count.
 *
 * A list a console shows is something to narrow, so every list page opens
 * with the same row: a search box on the left, the segmented filters beside
 * it, and anything else on the right.
 */

import { Search, X } from "lucide-react";
import { cn } from "@/lib/utils";

export function Toolbar({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("mb-3 flex flex-wrap items-center gap-2", className)}>{children}</div>;
}

export interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  /** The accessible name, and the placeholder. */
  label: string;
  /** Read out by the clear button. */
  clearLabel: string;
  className?: string;
}

export function SearchInput({ value, onChange, label, clearLabel, className }: SearchInputProps) {
  return (
    <div className={cn("relative w-full min-[600px]:w-[280px]", className)}>
      <Search size={14} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted" />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape" && value) {
            e.stopPropagation();
            onChange("");
          }
        }}
        aria-label={label}
        placeholder={label}
        className="h-9 w-full rounded-sm border border-line bg-bg2 pl-8 pr-8 text-[13px] text-text placeholder:text-muted focus:border-blue focus:outline-none [&::-webkit-search-cancel-button]:hidden"
      />
      {value && (
        <button
          type="button"
          onClick={() => onChange("")}
          aria-label={clearLabel}
          className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-muted hover:text-text"
        >
          <X size={13} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

/** Every word of the query appears somewhere in the haystack, case-insensitive. */
export function matchesQuery(query: string, ...haystack: (string | null | undefined)[]): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const text = haystack.filter(Boolean).join(" ").toLowerCase();
  return words.every((w) => text.includes(w));
}
