/** ⌘K: deploy a recipe, open a run, or jump to a page, from anywhere.
 *
 * Deploying was a hunt — find the card, open the drawer, scroll to the
 * button — and it is the thing an operator does most. The palette answers
 * "deploy bonsai" in three keystrokes and an Enter, and the same box finds a
 * run or a page. It opens on ⌘K / Ctrl+K, and on `openCommandPalette()` for
 * the buttons that offer it; it reads recipes and runs only when opened.
 *
 * A deploy item does not deploy: it opens the recipe's drawer on Recipes
 * (`/recipes?recipe=<id>`), where the options and the pre-flight are. The
 * palette is a way in, never a second deploy path to keep in step.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Box, Rocket, Search, Server, Terminal } from "lucide-react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { fetchDeployments, fetchNodes, fetchRecipes } from "@/lib/api";
import { isLiveRun } from "@/hooks/useDeployments";
import type { ClusterNode, Deployment, RecipeSummary } from "@/lib/types";

const OPEN_EVENT = "spark-pulse:command-palette";

/** Open the palette from a button. */
export function openCommandPalette(): void {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

export interface PaletteItem {
  id: string;
  group: "deploy" | "runs" | "nodes" | "pages";
  label: string;
  hint?: string;
  href: string;
}

const PAGES: { href: string; key: string }[] = [
  { href: "/", key: "nav.overview" },
  { href: "/jobs", key: "nav.runs" },
  { href: "/cluster", key: "nav.nodes" },
  { href: "/mods", key: "nav.mods" },
  { href: "/recipes", key: "nav.recipes" },
  { href: "/models", key: "nav.models" },
  { href: "/engines", key: "nav.engines" },
  { href: "/settings", key: "nav.settings" },
];

/** Every word of the query must appear somewhere in the label or hint. */
export function matches(item: PaletteItem, query: string): boolean {
  const hay = `${item.label} ${item.hint ?? ""}`.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => hay.includes(word));
}

export function buildItems(
  t: (key: string) => string,
  recipes: RecipeSummary[],
  runs: Deployment[],
  nodes: ClusterNode[],
): PaletteItem[] {
  const items: PaletteItem[] = [];
  for (const r of recipes) {
    if (r.fit?.verdict === "wont_run") continue;
    items.push({
      id: `deploy:${r.id}`,
      group: "deploy",
      label: r.name,
      hint: [r.engine || r.engines?.[0], r.model].filter(Boolean).join(" · "),
      href: `/recipes?recipe=${encodeURIComponent(r.id)}`,
    });
  }
  for (const run of [...runs].sort((a, b) => Number(isLiveRun(b)) - Number(isLiveRun(a)))) {
    items.push({
      id: `run:${run.id}`,
      group: "runs",
      label: run.name,
      hint: [run.phase || run.status, run.engine].filter(Boolean).join(" · "),
      href: `/jobs/${encodeURIComponent(run.id)}`,
    });
  }
  for (const n of nodes) {
    items.push({ id: `node:${n.id}`, group: "nodes", label: n.name, hint: n.address, href: "/cluster" });
  }
  for (const p of PAGES) items.push({ id: `page:${p.href}`, group: "pages", label: t(p.key), href: p.href });
  return items;
}

const GROUP_ICON = { deploy: Rocket, runs: Terminal, nodes: Server, pages: Box } as const;
const GROUP_KEY = {
  deploy: "palette.groupDeploy",
  runs: "palette.groupRuns",
  nodes: "palette.groupNodes",
  pages: "palette.groupPages",
} as const;
/** How many recipes, runs or nodes a group shows before the query narrows it. */
const PER_GROUP = 6;

export default function CommandPalette() {
  const t = useT();
  const navigate = useNavigate();
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [data, setData] = useState<{ recipes: RecipeSummary[]; runs: Deployment[]; nodes: ClusterNode[] }>({
    recipes: [],
    runs: [],
    nodes: [],
  });
  const inputRef = useRef<HTMLInputElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  const show = useCallback(() => {
    openerRef.current = document.activeElement as HTMLElement | null;
    setQuery("");
    setActive(0);
    setOpen(true);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    openerRef.current?.focus?.();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => {
          if (!o) openerRef.current = document.activeElement as HTMLElement | null;
          return !o;
        });
        setQuery("");
        setActive(0);
      }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_EVENT, show);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_EVENT, show);
    };
  }, [show]);

  // Read what it searches only when it opens; each source fails on its own.
  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    let cancelled = false;
    const settle = <T,>(p: Promise<T>, fallback: T) => p.catch(() => fallback);
    Promise.all([
      settle(fetchRecipes(), [] as RecipeSummary[]),
      settle(fetchDeployments(), [] as Deployment[]),
      settle(fetchNodes(), [] as ClusterNode[]),
    ]).then(([recipes, runs, nodes]) => {
      if (!cancelled) setData({ recipes, runs, nodes });
    });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const all = useMemo(() => buildItems(t, data.recipes, data.runs, data.nodes), [t, data]);
  const shown = useMemo(() => {
    const hits = all.filter((item) => matches(item, query));
    if (query.trim()) return hits;
    const counts: Record<string, number> = {};
    // Pages are a fixed, short list and never capped.
    return hits.filter(
      (item) => item.group === "pages" || (counts[item.group] = (counts[item.group] ?? 0) + 1) <= PER_GROUP,
    );
  }, [all, query]);

  useEffect(() => setActive(0), [query]);

  if (!open) return null;

  const go = (item: PaletteItem | undefined) => {
    if (!item) return;
    setOpen(false);
    navigate(item.href);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(shown.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(shown[active]);
    }
  };

  let lastGroup = "";
  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/50 px-4 pt-[12vh]" onMouseDown={close}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("palette.title")}
        className="w-full max-w-[600px] overflow-hidden rounded-md border border-line bg-surface shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2 border-b border-line px-3">
          <Search size={16} className="shrink-0 text-muted" aria-hidden="true" />
          <input
            ref={inputRef}
            id="command-palette-input"
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={shown[active] ? `${listId}-${active}` : undefined}
            aria-label={t("palette.title")}
            placeholder={t("palette.placeholder")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="h-12 w-full bg-transparent text-[15px] text-text outline-none placeholder:text-muted"
          />
          <kbd className="shrink-0 rounded border border-line px-1.5 font-mono text-[10px] text-muted">{t("palette.esc")}</kbd>
        </div>
        <ul id={listId} role="listbox" className="max-h-[50vh] overflow-y-auto py-1">
          {shown.length === 0 && <li className="px-4 py-6 text-center text-[13px] text-muted">{t("palette.empty")}</li>}
          {shown.map((item, i) => {
            const Icon = GROUP_ICON[item.group];
            const heading = item.group !== lastGroup ? t(GROUP_KEY[item.group]) : null;
            lastGroup = item.group;
            return (
              <li key={item.id} role="presentation">
                {heading && (
                  <p className="px-4 pb-1 pt-2 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-muted">{heading}</p>
                )}
                <div
                  id={`${listId}-${i}`}
                  role="option"
                  aria-selected={i === active}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => go(item)}
                  className={cn(
                    "mx-1 flex cursor-pointer items-center gap-3 rounded-sm px-3 py-2 text-[13.5px]",
                    i === active ? "bg-blue/15 text-text" : "text-text",
                  )}
                >
                  <Icon size={15} className="shrink-0 text-muted" aria-hidden="true" />
                  <span className="truncate font-medium">{item.label}</span>
                  {item.hint && <span className="min-w-0 truncate text-[12px] text-muted">{item.hint}</span>}
                  {i === active && <ArrowRight size={14} className="ml-auto shrink-0 text-muted" aria-hidden="true" />}
                </div>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
