/** Runtime configuration for the SPA.

Fetches /api/config on startup so the frontend can conditionally render
UI elements (login button, features, etc.) without needing separate builds.

See: https://kharkevich.org/2024/12/20/spa-runtime-config/
*/

interface AppConfig {
  auth_enabled: boolean;
  oidc_configured: boolean;
  mcp_enabled: boolean;
  cluster_enabled: boolean;
  simulation_mode: boolean;
}

let config: AppConfig | null = null;

export async function loadConfig(): Promise<AppConfig> {
  if (config) return config;
  try {
    const response = await fetch("/api/config");
    if (!response.ok) {
      throw new Error(`Failed to load configuration: ${response.statusText}`);
    }
    config = await response.json();
    return config as AppConfig;
  } catch {
    // Default to disabled auth if config fails to load
    const defaults: AppConfig = {
      auth_enabled: false,
      oidc_configured: false,
      mcp_enabled: true,
      cluster_enabled: false,
      simulation_mode: true,
    };
    config = defaults;
    return defaults;
  }
}

export function getConfig(): AppConfig {
  if (!config) {
    throw new Error(
      "Configuration not loaded yet. Call loadConfig() first."
    );
  }
  return config;
}

export function isAuthEnabled(): boolean {
  try {
    return getConfig().auth_enabled && getConfig().oidc_configured;
  } catch {
    return false;
  }
}
