import { useAuth } from "@/lib/auth";
import { doRefresh } from "@/lib/refresh";
import { type ThemeMode, getTheme, setTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { isAuthEnabled, isGitUpdateEnabled } from "@/lib/config";
import { Activity, Bot, Copyright, Database, ListChecks, LogOut, Menu, Moon, MoonStar, RotateCw, Settings, Sun, User, X, Zap } from "lucide-react";
import { SiGithub, SiPypi } from "@icons-pack/react-simple-icons";
import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import GitUpdateNotification from "@/components/Notifications";

const NAV = [
  { href: "/", label: "Recipes & Mods", icon: Zap },
  { href: "/jobs", label: "Jobs", icon: ListChecks },
  { href: "/monitoring", label: "Monitoring", icon: Activity },
  { href: "/cache", label: "Cache", icon: Database },
  { href: "/mcp", label: "MCP", icon: Bot },
  { href: "/settings", label: "Settings", icon: Settings },
];

// Internal header component with refresh + theme + auth
function HeaderInner() {
  const { isAuthenticated, user, login, logout, isConfigLoaded } = useAuth();
  const [authEnabled, setAuthEnabled] = useState(false);

  useEffect(() => {
    setAuthEnabled(isAuthEnabled());
    window.addEventListener("storage", () => setTheme(getTheme()));
  }, [isConfigLoaded]);

  return (
    <div className="hidden lg:flex fixed top-4 right-4 z-50 items-center gap-1.5">
      {isGitUpdateEnabled() && <GitUpdateNotification />}
      <button
        onClick={doRefresh}
        className="p-2 rounded-lg hover:bg-surface-hover transition-colors"
        title="Refresh"
      >
        <RotateCw size={16} />
      </button>
      <ThemeToggle />
      {authEnabled && isAuthenticated ? (
        <div className="flex items-center gap-2 ml-1">
          <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg bg-surface-hover text-sm">
            <User size={14} />
            {user?.name || user?.email || "User"}
          </span>
          <button onClick={logout} className="p-2 rounded-lg hover:bg-surface-hover transition-colors" title="Logout">
            <LogOut size={18} />
          </button>
        </div>
      ) : authEnabled ? (
        <button onClick={login} className="px-3 py-1.5 rounded-lg bg-primary hover:bg-primary-hover text-white text-sm font-medium transition-colors ml-1">
          Login
        </button>
      ) : null}
    </div>
  );
}

function ThemeToggle() {
  const [mode, setMode] = useState<ThemeMode>(getTheme());

  const cycle = () => {
    const next: ThemeMode = mode === "dark" ? "light" : mode === "light" ? "system" : "dark";
    setMode(next);
    setTheme(next);
  };

  const icon = mode === "dark" ? <Moon size={18} /> : mode === "light" ? <Sun size={18} /> : <MoonStar size={18} />;

  return (
    <button
      onClick={cycle}
      className="p-2 rounded-lg hover:bg-surface-hover transition-colors"
      title={`Theme: ${mode}`}
    >
      {icon}
    </button>
  );
}

export default function Layout({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  const [version, setVersion] = useState("");

  useEffect(() => {
    fetch("/version")
      .then((r) => r.json())
      .then((d) => setVersion(d.version))
      .catch(() => { });
  }, []);

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
            <Zap className="text-primary" size={28} />
            <div>
              <h1 className="font-bold text-lg leading-tight">Spark Pulse</h1>
              <p className="text-xs text-text-muted">{version}</p>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 p-4 space-y-1">
          {NAV.map((item) => {
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
                {item.label}
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
