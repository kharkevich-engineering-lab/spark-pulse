/** The app shell: a console.
 *
 * A sidebar listing every kind of thing this control plane manages — runs,
 * nodes, recipes, models, engines, collections — grouped as Operate, Library
 * and System, and a thin toolbar over the page with the command palette, the
 * language and the theme. It replaced a centred header of five groups, which
 * hid half the object types behind tabs: in a console every object type is
 * one click away and the address names it.
 *
 * Under 900px the sidebar folds into a menu button whose panel opens beneath
 * the toolbar (Escape closes it, a route change closes it, the body scroll
 * locks, focus goes in and comes back).
 */

import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useConfig } from "@/lib/config";
import { BrandFooter } from "@/components/BrandFooter";
import {
  BookOpen,
  Box,
  Cpu,
  Gauge,
  LayoutDashboard,
  LogOut,
  Menu,
  Puzzle,
  Search,
  Server,
  Settings,
  Terminal,
  User,
  X,
  type LucideIcon,
} from "lucide-react";
import CommandPalette, { openCommandPalette } from "@/components/CommandPalette";
import { PulseIcon } from "@/components/BrandIcons";
import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { Link, useLocation } from "react-router-dom";

/** One sidebar entry: where it takes you, and which routes it speaks for.
 *
 * `routes` is why this is more than a list of links — `/mcp` is Settings
 * whether or not it has its own entry, and an operator reading it should see
 * Settings lit rather than nothing at all. `flag` hides an entry whose
 * feature the backend has switched off.
 */
export interface NavGroup {
  href: string;
  labelKey: string;
  routes: string[];
  icon: LucideIcon;
  section: "operate" | "library" | "system";
  flag?: "benchmarking_enabled";
}

export const NAV_GROUPS: NavGroup[] = [
  { href: "/", labelKey: "nav.overview", routes: ["/"], icon: LayoutDashboard, section: "operate" },
  { href: "/jobs", labelKey: "nav.runs", routes: ["/jobs"], icon: Terminal, section: "operate" },
  {
    href: "/benchmarking",
    labelKey: "nav.benchmarks",
    routes: ["/benchmarking"],
    icon: Gauge,
    section: "operate",
    flag: "benchmarking_enabled",
  },
  { href: "/cluster", labelKey: "nav.nodes", routes: ["/cluster", "/monitoring"], icon: Server, section: "operate" },
  { href: "/recipes", labelKey: "nav.recipes", routes: ["/recipes", "/oci"], icon: BookOpen, section: "library" },
  { href: "/models", labelKey: "nav.models", routes: ["/models", "/cache"], icon: Box, section: "library" },
  { href: "/engines", labelKey: "nav.engines", routes: ["/engines"], icon: Cpu, section: "library" },
  { href: "/mods", labelKey: "nav.mods", routes: ["/mods"], icon: Puzzle, section: "library" },
  { href: "/settings", labelKey: "nav.settings", routes: ["/settings", "/mcp"], icon: Settings, section: "system" },
];

const SECTIONS: { id: NavGroup["section"]; labelKey: string }[] = [
  { id: "operate", labelKey: "nav.sectionOperate" },
  { id: "library", labelKey: "nav.sectionLibrary" },
  { id: "system", labelKey: "nav.sectionSystem" },
];

/** Which entry a path belongs to, or "" for a path no entry claims. */
export function activeGroup(pathname: string): string {
  return NAV_GROUPS.find((g) => g.routes.includes(pathname))?.href ?? "";
}

/** The header's hover wash, the hub's `color-mix(in srgb, var(--text) 7%)`. */
const HOVER = "hover:bg-[color-mix(in_srgb,var(--text)_7%,transparent)]";

/** 40×40, borderless, centred — the hub's `.icon-button`. */
const ICON_BUTTON = cn(
  "grid place-items-center w-10 h-10 rounded-sm border-0 bg-transparent text-muted transition-colors",
  HOVER,
  "hover:text-text",
);


/** Who is signed in, and the way out. Renders nothing when auth is off. */
function UserChip({ className }: { className?: string }) {
  const { isAuthenticated, user, logout } = useAuth();
  const { config } = useConfig();
  const t = useT();

  if (!(config?.auth_enabled ?? false) || !isAuthenticated) return null;

  return (
    <div className={cn("flex items-center gap-1", className)}>
      <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded-sm text-[13px] text-muted">
        <User size={14} />
        {user?.name || user?.email || "User"}
      </span>
      <button type="button" onClick={logout} className={ICON_BUTTON} aria-label={t("a11y.logout")} title={t("a11y.logout")}>
        <LogOut size={18} />
      </button>
    </div>
  );
}

export default function Layout({ children }: { children: React.ReactNode }) {
  const t = useT();
  const { config } = useConfig();
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const [version, setVersion] = useState("");
  const menuRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    fetch("/version")
      .then((r) => r.json())
      .then((d) => setVersion(d.version))
      .catch(() => {});
  }, []);

  const current = useMemo(() => activeGroup(location.pathname), [location.pathname]);
  const groups = useMemo(
    () => NAV_GROUPS.filter((g) => !g.flag || Boolean(config?.[g.flag])),
    [config],
  );

  // A route change puts the menu away: it covers the page it just navigated to.
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  // Escape closes it, the same key that closes every dialog in the app.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  // The page behind an open menu must not scroll under it.
  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  // Focus follows the menu in and back out again.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open) {
      menuRef.current?.querySelector<HTMLElement>("a, button")?.focus();
    } else if (wasOpen.current) {
      menuButtonRef.current?.focus();
    }
    wasOpen.current = open;
  }, [open]);

  const renderNav = useCallback(
    (testId: string) => (
      <nav data-testid={testId} aria-label={t("a11y.primaryNav")} className="flex flex-col gap-4">
        {SECTIONS.map((section) => {
          const items = groups.filter((g) => g.section === section.id);
          if (items.length === 0) return null;
          return (
            <div key={section.id} className="flex flex-col gap-0.5">
              <p className="px-2.5 pb-1 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-muted">
                {t(section.labelKey)}
              </p>
              {items.map((group) => {
                const active = current === group.href;
                const Icon = group.icon;
                return (
                  <Link
                    key={group.href}
                    to={group.href}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "flex items-center gap-2.5 rounded-sm px-2.5 py-1.5 text-[13.5px] font-medium no-underline hover:no-underline transition-colors",
                      active ? "bg-surface text-text shadow-[inset_2px_0_0_var(--blue)]" : cn("text-muted hover:text-text", HOVER),
                    )}
                  >
                    <Icon size={16} aria-hidden="true" className="shrink-0" />
                    {t(group.labelKey)}
                  </Link>
                );
              })}
            </div>
          );
        })}
      </nav>
    ),
    [current, groups, t],
  );

  const brand = (
    <Link
      to="/"
      aria-label={t("a11y.home")}
      className="inline-flex items-center gap-2.5 text-text no-underline hover:no-underline"
    >
      <PulseIcon className="text-blue2 shrink-0" size={26} />
      <span className="flex flex-col leading-[1.05]">
        <strong className="text-[14.5px] font-bold tracking-[-0.01em] whitespace-nowrap">{t("brand.product")}</strong>
        {version && (
          <small className="mt-[3px] text-[9.5px] font-medium uppercase tracking-[0.2em] text-muted">{version}</small>
        )}
      </span>
    </Link>
  );

  return (
    <div className="min-h-screen bg-bg text-text min-[900px]:grid min-[900px]:grid-cols-[220px_minmax(0,1fr)]">
      <aside className="hidden min-[900px]:flex sticky top-0 h-screen flex-col gap-6 overflow-y-auto border-r border-line bg-bg2 px-3 py-4">
        <div className="px-1.5">{brand}</div>
        {renderNav("primary-nav")}
        <div className="mt-auto flex flex-col gap-3 px-1">
          <UserChip />
          <BrandFooter compact />
        </div>
      </aside>

      <div className="flex min-h-screen min-w-0 flex-col">
        <header
          className={cn(
            "sticky top-0 z-50 flex items-center gap-2 h-14 px-4 min-[900px]:px-6",
            "bg-[color-mix(in_srgb,var(--bg)_86%,transparent)] backdrop-blur-[18px] border-b border-line",
          )}
        >
          <div className="min-[900px]:hidden">{brand}</div>
          <button
            type="button"
            onClick={openCommandPalette}
            aria-label={t("palette.open")}
            className={cn(
              "ml-auto min-[900px]:ml-0 inline-flex items-center gap-2 h-9 rounded-sm border border-line px-2.5 text-[13px] text-muted transition-colors min-[900px]:w-[360px]",
              HOVER,
              "hover:text-text",
            )}
          >
            <Search size={15} aria-hidden="true" />
            <span className="hidden min-[900px]:inline">{t("palette.trigger")}</span>
            <kbd className="hidden min-[900px]:inline ml-auto rounded border border-line px-1 font-mono text-[10px]">⌘K</kbd>
          </button>
          <button
            ref={menuButtonRef}
            type="button"
            className={cn(ICON_BUTTON, "min-[900px]:hidden")}
            aria-label={t("a11y.menu")}
            aria-expanded={open}
            aria-controls="mobile-menu"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? <X size={22} /> : <Menu size={22} />}
          </button>
        </header>

        {open && (
          <div
            id="mobile-menu"
            ref={menuRef}
            className={cn(
              "fixed top-14 left-0 right-0 z-40 flex flex-col gap-4 min-[900px]:hidden",
              "bg-bg border-b border-line px-4 pt-3 pb-5 max-h-[calc(100vh-56px)] overflow-y-auto",
            )}
          >
            {renderNav("mobile-nav")}
            <div className="flex items-center justify-between gap-2 border-t border-line pt-3">
              <UserChip />
            </div>
            <BrandFooter compact />
          </div>
        )}

        <main className="flex-1 w-full max-w-[1600px] px-4 pt-5 pb-12 min-[900px]:px-6 min-[900px]:pt-5 min-[900px]:pb-14">
          {children}
        </main>
      </div>

      <CommandPalette />
    </div>
  );
}
