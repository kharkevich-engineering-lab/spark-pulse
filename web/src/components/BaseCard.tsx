/** Base card component used across all pages.

Shared layout: icon + title at top, description, optional badge row, chevron arrow.
*/

import { ChevronRight, Trash2 } from "lucide-react";

interface BaseCardProps {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  description?: string;
  badges?: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
  /** Shown as a trash control in the corner. Absent means this card names
   *  something the operator did not create and cannot remove — a bundled
   *  recipe lives inside the package. */
  onDelete?: () => void;
  /** What the delete control says it will do, for a screen reader and a
   *  tooltip. Required with `onDelete` so no card offers an unlabelled bin. */
  deleteLabel?: string;
  /** Shown as a native tooltip when there is no `onDelete`, so a card with
   *  nothing to remove reads as deliberate ("Managed recipe") rather than
   *  broken. Ignored when `onDelete` is set — the delete button already
   *  carries its own tooltip via `deleteLabel`. */
  hint?: string;
}

export default function BaseCard({ icon, title, subtitle, description, badges, onClick, disabled, className, onDelete, deleteLabel, hint }: BaseCardProps) {
  const interactive = !disabled;

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!interactive) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onClick();
    }
  };

  return (
    <div
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      aria-disabled={disabled || undefined}
      onClick={interactive ? onClick : undefined}
      onKeyDown={handleKeyDown}
      title={onDelete ? undefined : hint}
      className={`text-left p-5 rounded-md bg-surface border transition-colors ${
        disabled
          ? "opacity-50 cursor-not-allowed border-border"
          : "border-border hover:border-primary/50 hover:bg-surface-hover cursor-pointer group"
      } ${className ?? ""}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            {icon}
            <span className="font-semibold text-sm group-hover:text-blue2 transition-colors truncate">
              {title}
            </span>
            {subtitle && <span className="text-xs text-text-muted font-mono truncate">{subtitle}</span>}
          </div>
          {description && (
            <p className="text-sm text-text-muted leading-snug mt-1 line-clamp-2">{description}</p>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0 mt-0.5">
          {onDelete && (
            // `stopPropagation`, because the whole card is the open action and
            // a delete that also opened what it deleted would be a trap.
            <button
              type="button"
              aria-label={deleteLabel}
              title={deleteLabel}
              onClick={(e) => {
                e.stopPropagation();
                onDelete();
              }}
              className="p-1 rounded text-text-muted hover:text-danger hover:bg-danger/10 transition-colors"
            >
              <Trash2 size={15} />
            </button>
          )}
          {!disabled && <ChevronRight size={16} className="text-text-muted group-hover:text-blue2 transition-colors" />}
        </div>
      </div>
      {badges && <div className="flex flex-wrap gap-1.5 mt-3">{badges}</div>}
    </div>
  );
}
