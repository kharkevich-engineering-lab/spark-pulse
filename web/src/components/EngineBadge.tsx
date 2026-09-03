import type { EngineSummary } from "@/lib/types";

interface EngineBadgeProps {
  engine: string;
  variant?: string;
  isDefault?: boolean;
  enabled?: boolean;
}

const COLORS: Record<string, string> = {
  vllm: "bg-primary/20 text-primary border-primary/30",
  sglang: "bg-success/20 text-success border-success/30",
};

/** Small pill naming an engine (and its variant when it is not the default one). */
export default function EngineBadge({ engine, variant, isDefault, enabled = true }: EngineBadgeProps) {
  const c = enabled ? COLORS[engine] || "bg-text-muted/10 text-text-muted border-text-muted/30" : "bg-text-muted/10 text-text-muted border-text-muted/30 opacity-60";
  const label = variant && variant !== "default" ? `${engine} · ${variant}` : engine;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${c}`}>
      {label}
      {isDefault && <span className="text-[10px] uppercase tracking-wide opacity-70">default</span>}
      {!enabled && <span className="text-[10px] uppercase tracking-wide opacity-70">off</span>}
    </span>
  );
}

interface EngineListProps {
  engines: EngineSummary[];
  defaultEngine: string;
}

/** Read-only list of the engines the registry knows about. */
export function EngineList({ engines, defaultEngine }: EngineListProps) {
  if (engines.length === 0) {
    return <p className="text-sm text-text-muted">No engines available.</p>;
  }
  return (
    <ul className="space-y-3">
      {engines.map((e) => (
        <li key={e.key} className="space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <EngineBadge engine={e.engine} variant={e.variant} enabled={e.enabled} isDefault={e.engine === defaultEngine && e.variant === "default"} />
            <span className="text-xs text-text-muted font-mono">v{e.version}</span>
            {e.verified.length > 0 && <span className="text-[10px] uppercase tracking-wide text-success">verified</span>}
          </div>
          <p className="text-xs text-text-muted font-mono break-all">{e.digest ? `${e.image}@${e.digest.slice(0, 19)}…` : e.image_ref}</p>
          <p className="text-xs text-text-muted">
            {Object.entries(e.capabilities)
              .filter(([, v]) => v)
              .map(([k]) => k)
              .join(", ") || "no capabilities declared"}
            {" · "}
            <span className="font-mono">:{e.ports.api}</span>
            {e.ports.rendezvous ? <span className="font-mono"> / :{e.ports.rendezvous}</span> : null}
          </p>
        </li>
      ))}
    </ul>
  );
}
