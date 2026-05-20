interface StatusBadgeProps { status: string }
const COLORS: Record<string, string> = {
  running: "bg-success/20 text-success border-success/30",
  stopped: "bg-text-muted/10 text-text-muted border-text-muted/30",
  error: "bg-danger/20 text-danger border-danger/30",
  pending: "bg-warning/20 text-warning border-warning/30",
};

export default function StatusBadge({ status }: StatusBadgeProps) {
  const c = COLORS[status.toLowerCase()] || COLORS.stopped;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${c}`}>
      <span className="w-1.5 h-1.5 rounded-full bg-current" style={{ animation: status === "running" ? "pulse 2s infinite" : "none" }} />
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}
