/** Runtime configuration: `/api/config`, fetched once and shared.
 *
 * Every feature gate in the SPA reads this — the Benchmarking route, the login
 * button, the experimental marking on Cluster. Two properties matter and both
 * are about failure: the fetch happens exactly once however many components
 * ask, and a backend that cannot answer must not leave the app with no config
 * at all. The module caches in a module-level variable, so each test imports
 * it fresh rather than inheriting the previous test's cache.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

type ConfigModule = typeof import("@/lib/config");

/** A fresh copy of the module, with its cache empty. */
async function freshConfig(): Promise<ConfigModule> {
  vi.resetModules();
  return import("@/lib/config");
}

const SERVED = {
  auth_enabled: true,
  mcp_enabled: false,
  cluster_enabled: true,
  cluster_experimental: false,
  benchmarking_enabled: true,
  simulation_mode: false,
  runtime: "native",
};

const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

describe("loadConfig", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("reads the served configuration", async () => {
    const { loadConfig } = await freshConfig();
    fetchMock().mockResolvedValue({ ok: true, json: async () => SERVED });

    expect(await loadConfig()).toEqual(SERVED);
    expect(fetchMock()).toHaveBeenCalledWith("/api/config");
  });

  // Every page mounts components that ask for config; asking the backend once
  // per component would put a request storm behind every navigation.
  //
  // The cache is a resolved value, not an in-flight promise, so this is the
  // guarantee for callers that ask after the first has answered. Callers that
  // ask *concurrently* do each issue a request — ConfigProvider and
  // AuthProvider mount together and both call this, so a cold start makes two.
  it("fetches once for every caller after the first has answered", async () => {
    const { loadConfig } = await freshConfig();
    fetchMock().mockResolvedValue({ ok: true, json: async () => SERVED });

    await loadConfig();
    await loadConfig();
    await loadConfig();

    expect(fetchMock()).toHaveBeenCalledTimes(1);
  });

  // A config the SPA cannot load must not blank the app: the defaults are
  // the conservative reading — no auth, and Cluster still marked unproven.
  it("falls back to defaults when the backend answers with an error status", async () => {
    const { loadConfig } = await freshConfig();
    fetchMock().mockResolvedValue({ ok: false, statusText: "Bad Gateway", json: async () => ({}) });

    const config = await loadConfig();
    expect(config.auth_enabled).toBe(false);
    expect(config.cluster_experimental).toBe(true);
    expect(config.runtime).toBe("native");
  });

  it("falls back to defaults when the request never completes", async () => {
    const { loadConfig } = await freshConfig();
    fetchMock().mockRejectedValue(new Error("network down"));

    await expect(loadConfig()).resolves.toMatchObject({ auth_enabled: false });
  });
});

describe("getConfig", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // Synchronous callers run before the fetch resolves; they get defaults
  // rather than a null they would each have to guard.
  it("answers with defaults before anything has been loaded", async () => {
    const { getConfig } = await freshConfig();
    expect(getConfig().runtime).toBe("native");
    expect(getConfig().benchmarking_enabled).toBe(false);
  });

  it("answers with the served config once it has loaded", async () => {
    const { getConfig, loadConfig } = await freshConfig();
    fetchMock().mockResolvedValue({ ok: true, json: async () => SERVED });

    await loadConfig();

    expect(getConfig()).toEqual(SERVED);
  });

  it("hands out a copy of the defaults, so a caller cannot corrupt them", async () => {
    const { getConfig } = await freshConfig();
    getConfig().benchmarking_enabled = true;
    expect(getConfig().benchmarking_enabled).toBe(false);
  });
});

describe("ConfigProvider", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** A consumer that renders the two things the context exposes. */
  function Probe({ useConfig }: { useConfig: ConfigModule["useConfig"] }) {
    const { config, configLoaded } = useConfig();
    return (
      <div>
        <span data-testid="loaded">{String(configLoaded)}</span>
        <span data-testid="runtime">{config ? config.runtime : "—"}</span>
      </div>
    );
  }

  it("starts unloaded and publishes the config once it arrives", async () => {
    const { ConfigProvider, useConfig } = await freshConfig();
    fetchMock().mockResolvedValue({ ok: true, json: async () => SERVED });

    render(
      <ConfigProvider>
        <Probe useConfig={useConfig} />
      </ConfigProvider>,
    );

    expect(screen.getByTestId("loaded")).toHaveTextContent("false");
    await waitFor(() => expect(screen.getByTestId("loaded")).toHaveTextContent("true"));
    expect(screen.getByTestId("runtime")).toHaveTextContent("native");
  });

  // Nothing renders config-dependent UI until `configLoaded` is true, so a
  // failed load still has to flip it — otherwise the app hangs on a spinner.
  it("reports loaded even when the backend could not be reached", async () => {
    const { ConfigProvider, useConfig } = await freshConfig();
    fetchMock().mockRejectedValue(new Error("network down"));

    render(
      <ConfigProvider>
        <Probe useConfig={useConfig} />
      </ConfigProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("loaded")).toHaveTextContent("true"));
  });

  // Navigating away before /api/config answers unmounts the provider; setting
  // state afterwards is the React warning nobody ever gets round to fixing.
  it("does not publish a config into a provider that has already unmounted", async () => {
    const { ConfigProvider, useConfig } = await freshConfig();
    let answer: (value: { ok: boolean; json: () => Promise<unknown> }) => void = () => {};
    fetchMock().mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const warnings = vi.spyOn(console, "error").mockImplementation(() => {});

    const { unmount } = render(
      <ConfigProvider>
        <Probe useConfig={useConfig} />
      </ConfigProvider>,
    );
    unmount();
    answer({ ok: true, json: async () => SERVED });
    await Promise.resolve();

    expect(warnings).not.toHaveBeenCalled();
    warnings.mockRestore();
  });

  it("gives a consumer outside the provider a null config rather than throwing", async () => {
    const { useConfig } = await freshConfig();
    render(<Probe useConfig={useConfig} />);
    expect(screen.getByTestId("runtime")).toHaveTextContent("—");
    expect(screen.getByTestId("loaded")).toHaveTextContent("false");
  });
});
