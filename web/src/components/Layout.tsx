/** The app shell: one sticky header, five groups, and the page under it.
 *
 * It was a 256px sidebar listing eleven routes, with a `fixed top-4 left-4`
 * hamburger and a `fixed top-4 right-4` user chip floating over whatever the
 * page had drawn there. Below `lg:` the chip was hidden outright, so an
 * operator on a phone had no way to sign out at all.
 *
 * This is hub.kharkevich.com's header instead: brand left, the five groups
 * centred, the actions right, and under 900px one menu button that opens a
 * panel in the flow beneath the header rather than a drawer over the page.
 * Eleven routes became five groups because a nav is a map, not an index — the
 * routes are unchanged and a group lights up for any of its members.
 */

import { useAuth } from "@/lib/auth";
import { useI18n, useT, LANGUAGES } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { ExperimentalBadge } from "@/components/Experimental";
import { MULTI_NODE_BADGE_TITLE } from "@/lib/experimental";
import { useConfig } from "@/lib/config";
import { BrandFooter } from "@/components/BrandFooter";
import { getTheme, setTheme, type ThemeMode } from "@/lib/theme";
import { LogOut, Menu, Moon, Sun, User, X } from "lucide-react";
import { PulseIcon, SunMoonIcon } from "@/components/BrandIcons";
import { useCallback, useEffect, useRef, useState, useMemo } from "react";
import { Link, useLocation } from "react-router-dom";

/** A header group: where it takes you, and which routes it speaks for.
 *
 * `routes` is the whole reason this is a list of groups rather than a list of
 * links — Monitoring is part of Fleet whether or not it has its own entry, and
 * an operator reading `/monitoring` should see Fleet lit up rather than
 * nothing at all.
 */
export interface NavGroup {
  href: string;
  labelKey: string;
  routes: string[];
  experimental?: boolean;
}

export const NAV_GROUPS: NavGroup[] = [
  { href: "/", labelKey: "nav.deploy", routes: ["/"] },
  { href: "/jobs", labelKey: "nav.runs", routes: ["/jobs", "/benchmarking"] },
  {
    href: "/cluster",
    labelKey: "nav.fleet",
    routes: ["/cluster", "/monitoring"],
    experimental: true,
  },
  { href: "/models", labelKey: "nav.library", routes: ["/models", "/engines", "/cache", "/oci"] },
  { href: "/settings", labelKey: "nav.settings", routes: ["/settings", "/mcp"] },
];

/** Which group a path belongs to, or "" for a path no group claims. */
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

const THEME_ORDER: ThemeMode[] = ["system", "light", "dark"];
const THEME_LABEL: Record<ThemeMode, string> = {
  system: "preferences.themeSystem",
  light: "preferences.themeLight",
  dark: "preferences.themeDark",
};

/** system → light → dark → system, in one 40×40 button.
 *
 * The theme also lives on Settings → Preferences, which is where it is
 * *explained*. This is the same preference one click away, because changing it
 * is something an operator does while looking at the page they want changed.
 */
function ThemeCycler() {
  const t = useT();
  const [mode, setMode] = useState<ThemeMode>(() => getTheme());

  const cycle = () => {
    const next = THEME_ORDER[(THEME_ORDER.indexOf(mode) + 1) % THEME_ORDER.length];
    setTheme(next);
    setMode(next);
  };

  const Icon = mode === "light" ? Sun : mode === "dark" ? Moon : SunMoonIcon;
  return (
    <button
      type="button"
      onClick={cycle}
      className={ICON_BUTTON}
      aria-label={t("a11y.theme", { mode: t(THEME_LABEL[mode]) })}
    >
      <Icon size={20} />
    </button>
  );
}

/** EN/FR, as the hub draws it: both halves always visible, the current one in
 *  full contrast. One control, so the choice is a single target. */
function LanguageSwitch() {
  const { language, setLanguage } = useI18n();
  const t = useT();
  const other = LANGUAGES.find((l) => l.id !== language) ?? LANGUAGES[0];

  return (
    <button
      type="button"
      onClick={() => setLanguage(other.id)}
      aria-label={t("a11y.language", { language: other.label })}
      className={cn(
        "inline-flex gap-0.5 px-2.5 py-2 rounded-sm border-0 bg-transparent",
        "text-[12px] font-semibold tracking-[0.06em] text-muted transition-colors",
        HOVER,
      )}
    >
      {LANGUAGES.map((l, i) => (
        <span key={l.id}>
          {i > 0 && <span aria-hidden="true">/</span>}
          <span aria-current={l.id === language ? "true" : undefined} className={cn(l.id === language && "text-text")}>
            {l.id.toUpperCase()}
          </span>
        </span>
      ))}
    </button>
  );
}

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
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const [version, setVersion] = useState("");
  const { config } = useConfig();
  const clusterExperimental = config?.cluster_experimental ?? true;
  const menuRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    fetch("/version")
      .then((r) => r.json())
      .then((d) => setVersion(d.version))
      .catch(() => {});
  }, []);

  const current = useMemo(() => activeGroup(location.pathname), [location.pathname]);

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

  // Focus follows the menu in and back out again, so a keyboard user is never
  // left tabbing through a panel that is no longer on screen.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open) {
      menuRef.current?.querySelector<HTMLElement>("a, button")?.focus();
    } else if (wasOpen.current) {
      menuButtonRef.current?.focus();
    }
    wasOpen.current = open;
  }, [open]);

  const renderLink = useCallback(
    (group: NavGroup, variant: "desktop" | "mobile") => {
      const active = current === group.href;
      return (
        <Link
          key={group.href}
          to={group.href}
          aria-current={active ? "page" : undefined}
          className={cn(
            "inline-flex items-center gap-2 text-[13.5px] font-medium no-underline hover:no-underline transition-colors",
            HOVER,
            active ? "text-text" : "text-muted hover:text-text",
            variant === "desktop"
              ? cn("px-3 py-2 rounded-sm", active && "rounded-none shadow-[inset_0_-2px_0_var(--blue)]")
              : "px-1 py-3.5 border-b border-line rounded-none",
          )}
        >
          {t(group.labelKey)}
          {group.experimental && clusterExperimental && (
            <ExperimentalBadge title={MULTI_NODE_BADGE_TITLE} />
          )}
        </Link>
      );
    },
    [current, clusterExperimental, t],
  );

  return (
    <div className="min-h-screen flex flex-col bg-bg text-text">
      <header
        className={cn(
          "sticky top-0 z-50 flex items-center gap-6 h-[72px] px-5 min-[900px]:px-7",
          "bg-[color-mix(in_srgb,var(--bg)_82%,transparent)] backdrop-blur-[18px]",
          "border-b border-line",
        )}
      >
        <Link
          to="/"
          aria-label={t("a11y.home")}
          className="inline-flex items-center gap-2.5 text-text no-underline hover:no-underline min-[900px]:min-w-[260px]"
        >
          <PulseIcon className="text-blue2 shrink-0" size={34} />
          <span className="flex flex-col leading-[1.05]">
            <strong className="text-[15px] font-bold tracking-[-0.01em] whitespace-nowrap max-[520px]:text-[13px]">
              {t("brand.product")}
            </strong>
            {version && (
              <small className="mt-[3px] text-[9.5px] font-medium uppercase tracking-[0.22em] text-muted max-[520px]:text-[8.5px]">
                {version}
              </small>
            )}
          </span>
        </Link>

        <nav
          data-testid="primary-nav"
          aria-label={t("a11y.primaryNav")}
          className="hidden min-[900px]:flex gap-1 mx-auto"
        >
          {NAV_GROUPS.map((group) => renderLink(group, "desktop"))}
        </nav>

        <div className="flex items-center gap-1 ml-auto min-[900px]:ml-0 min-[900px]:min-w-[200px] min-[900px]:justify-end">
          <div className="hidden min-[900px]:flex items-center gap-1">
            <UserChip />
            <LanguageSwitch />
            <ThemeCycler />
          </div>
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
        </div>
      </header>

      {open && (
        <div
          id="mobile-menu"
          ref={menuRef}
          className={cn(
            "fixed top-[72px] left-0 right-0 z-40 flex flex-col min-[900px]:hidden",
            "bg-bg border-b border-line px-5 pt-3 pb-5 max-h-[calc(100vh-72px)] overflow-y-auto",
          )}
        >
          <nav data-testid="mobile-nav" aria-label={t("a11y.primaryNav")} className="flex flex-col">
            {NAV_GROUPS.map((group) => renderLink(group, "mobile"))}
          </nav>
          <div className="flex items-center justify-between gap-2 pt-4">
            <UserChip />
            <div className="flex items-center gap-1 ml-auto">
              <LanguageSwitch />
              <ThemeCycler />
            </div>
          </div>
        </div>
      )}

      <main className="flex-1 w-full max-w-[1100px] mx-auto px-5 pt-8 pb-[60px] min-[900px]:px-7 min-[900px]:pt-12 min-[900px]:pb-20">
        {children}
      </main>

      <BrandFooter />
    </div>
  );
}
