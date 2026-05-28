/** Runtime configuration for the SPA.

Fetches /api/config on startup so the frontend can conditionally render
UI elements (login button, features, etc.) without needing separate builds.
*/

import { createContext, useContext, useEffect, useState } from "react";

export interface AppConfig {
  auth_enabled: boolean;
  oidc_configured: boolean;
  mcp_enabled: boolean;
  cluster_enabled: boolean;
  git_update_enabled: boolean;
  benchmarking_enabled: boolean;
  simulation_mode: boolean;
}

const DEFAULT_CONFIG: AppConfig = {
  auth_enabled: false,
  oidc_configured: false,
  mcp_enabled: true,
  cluster_enabled: false,
  git_update_enabled: true,
  benchmarking_enabled: false,
  simulation_mode: true,
};

let cachedConfig: AppConfig | null = null;

/** Load config from /api/config. Idempotent — second call returns cached value. */
export async function loadConfig(): Promise<AppConfig> {
  if (cachedConfig) return cachedConfig;
  try {
    const response = await fetch("/api/config");
    if (!response.ok) {
      throw new Error(`Failed to load configuration: ${response.statusText}`);
    }
    cachedConfig = await response.json();
    return cachedConfig as AppConfig;
  } catch {
    cachedConfig = { ...DEFAULT_CONFIG };
    return cachedConfig as AppConfig;
  }
}

export function getConfig(): AppConfig {
  if (!cachedConfig) {
    return { ...DEFAULT_CONFIG };
  }
  return cachedConfig;
}

export function isAuthEnabled(): boolean {
  return getConfig().auth_enabled && getConfig().oidc_configured;
}

export function isGitUpdateEnabled(): boolean {
  return getConfig().git_update_enabled;
}

export function isBenchmarkingEnabled(): boolean {
  return getConfig().benchmarking_enabled;
}

// ── React context for config ───────────────────────────────────────────────

interface ConfigContextValue {
  config: AppConfig | null;
  configLoaded: boolean;
}

const ConfigContext = createContext<ConfigContextValue>({
  config: null,
  configLoaded: false,
});

/** Provider that fetches /api/config on mount and exposes it via context. */
export function ConfigProvider({ children }: { children: React.ReactNode }) {
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    loadConfig()
      .then((c) => { if (!cancelled) { setCfg(c); setLoaded(true); } })
      .catch(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, []);

  return (
    <ConfigContext.Provider value={{ config: cfg, configLoaded: loaded }}>
      {children}
    </ConfigContext.Provider>
  );
}

/** Read config from context. Returns null while loading. */
export function useConfig(): ConfigContextValue {
  return useContext(ConfigContext);
}
