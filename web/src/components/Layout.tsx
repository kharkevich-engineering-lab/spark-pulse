import { useAuth } from "@/lib/auth";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { ExperimentalBadge } from "@/components/Experimental";
import { MULTI_NODE_BADGE_TITLE } from "@/lib/experimental";
import { useConfig } from "@/lib/config";
import { Activity, Bot, Boxes, Copyright, Database, Flame, Layers, ListChecks, LogOut, Menu, Package, Settings, User, X, Server } from "lucide-react";
import { PulseIcon } from "@/components/BrandIcons";
import { SiGithub, SiPypi } from "@icons-pack/react-simple-icons";
import { useEffect, useState, useMemo } from "react";
import { Link, useLocation } from "react-router-dom";

/** Anything that draws itself at a size: our own marks and lucide's alike.
 *  `typeof PulseIcon` is too narrow — lucide exports forwardRef components,
 *  whose return type is `ReactNode` rather than `Element`. */
type NavIcon = React.ComponentType<{ size?: number; className?: string }>;

/** The sidebar, by translation key rather than by label.
 *
 * The route is the identity; what it is *called* depends on the language, so
 * the label cannot be baked into this list. */
const NAV: { href: string; labelKey: string; icon: NavIcon; experimental?: boolean }[] = [
  { href: "/", labelKey: "nav.recipes", icon: PulseIcon },
  { href: "/jobs", labelKey: "nav.inference", icon: ListChecks },
  { href: "/cluster", labelKey: "nav.cluster", icon: Server, experimental: true },
  { href: "/benchmarking", labelKey: "nav.benchmarking", icon: Flame },
  { href: "/monitoring", labelKey: "nav.monitoring", icon: Activity },
  { href: "/models", labelKey: "nav.models", icon: Boxes },
  { href: "/engines", labelKey: "nav.engines", icon: Layers },
  { href: "/cache", labelKey: "nav.cache", icon: Database },
  { href: "/mcp", labelKey: "nav.mcp", icon: Bot },
  { href: "/oci", labelKey: "nav.oci", icon: Package },
  { href: "/settings", labelKey: "nav.settings", icon: Settings },
];

/** The header: who you are, and the way out.
 *
 * It used to carry a red/green SSE dot, a refresh button and a theme cycler.
 * The dot reported a connection nothing on the page depended on and read as
 * an error indicator; refresh is what the browser's own reload does; and the
 * theme is a preference, which belongs with the other preferences rather than
 * one unlabelled click away from every page. See the Settings page.
 */
function HeaderInner() {
  const { isAuthenticated, user, logout } = useAuth();
  const { config } = useConfig();
  const t = useT();
  const authEnabled = config?.auth_enabled ?? false;

  if (!authEnabled || !isAuthenticated) return null;

  return (
    <div className="hidden lg:flex fixed top-4 right-4 z-50 items-center gap-2">
      <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-surface-hover text-sm">
        <User size={14} />
        {user?.name || user?.email || "User"}
      </span>
      <button onClick={logout} className="p-2 rounded-lg hover:bg-surface-hover transition-colors" title={t("a11y.logout")}>
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
  const benchmarkingEnabled = config?.benchmarking_enabled ?? false;
  const clusterExperimental = config?.cluster_experimental ?? true;

  useEffect(() => {
    fetch("/version")
      .then((r) => r.json())
      .then((d) => setVersion(d.version))
      .catch(() => { });
  }, []);

  const navItems = useMemo(() => NAV.filter((item) => {
    if (item.href === "/benchmarking") return benchmarkingEnabled;
    return true;
  }), [benchmarkingEnabled]);

  return (
    <div className="flex h-screen bg-bg text-text">
      {/* Mobile menu button */}
      <button
        className="lg:hidden fixed top-4 left-4 z-50 p-2 rounded-lg bg-surface border border-border hover:border-border-hover"
        onClick={() => setOpen(!open)}
      >
        {open ? <X size={20} /> : <Menu size={20} />}
      </button>

      {/* Desktop header */}
      <HeaderInner />

      {/* Sidebar overlay on mobile */}
      {open && <div className="lg:hidden fixed inset-0 bg-black/50 z-40" onClick={() => setOpen(false)} />}

      {/* Sidebar */}
      <aside
        className={cn(
          "fixed lg:static inset-y-0 left-0 z-40 w-64 bg-surface border-r border-border flex flex-col transition-transform duration-200",
          open ? "translate-x-0" : "-translate-x-full lg:translate-x-0"
        )}
      >
        {/* Logo */}
        <div className="p-6 border-b border-border">
          <div className="flex items-center gap-3">
            <PulseIcon className="text-primary" size={28} />
            <div>
              <h1 className="font-bold text-lg leading-tight">Spark Pulse</h1>
              <p className="text-xs text-text-muted">{version}</p>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 p-4 space-y-1">
          {navItems.map((item) => {
            const active = location.pathname === item.href;
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                to={item.href}
                onClick={() => setOpen(false)}
                className={cn(
                  "flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors",
                  active
                    ? "bg-primary/10 text-primary"
                    : "text-text-muted hover:text-text hover:bg-surface-hover"
                )}
              >
                <Icon size={18} />
                <span className="flex-1">{t(item.labelKey)}</span>
                {item.experimental && clusterExperimental && (
                  <ExperimentalBadge title={MULTI_NODE_BADGE_TITLE} />
                )}
              </Link>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="p-4 border-t border-border text-xs text-text-muted space-y-1">
          <a
            href="https://kharkevich.com"
            target="_blank"
            rel="noopener"
            className="inline-flex items-center gap-1.5 hover:text-text transition-colors"
          >
            <Copyright size={12} />
            {new Date().getFullYear()} Kharkevich Engineering Lab
          </a>
          <div className="flex items-center gap-4">
            <a
              href="https://github.com/kharkevich-engineering-lab/spark-pulse"
              target="_blank"
              rel="noopener"
              className="inline-flex items-center gap-1.5 hover:text-text transition-colors"
            >
              <SiGithub size={12} />
              GitHub
            </a>
            <a
              href="https://pypi.org/project/spark-pulse/"
              target="_blank"
              rel="noopener"
              className="inline-flex items-center gap-1.5 hover:text-text transition-colors"
            >
              <SiPypi size={12} />
              PyPI
            </a>
          </div>
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto">
        <div className="p-6 lg:p-8">{children}</div>
      </main>
    </div>
  );
}
