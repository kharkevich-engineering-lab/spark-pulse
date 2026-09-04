import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fetchEngines, fetchEngine, refreshEngines, renderLaunch } from "@/lib/api";

const ok = (body: unknown) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);

describe("engine api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

  it("fetchEngines GETs /api/engines", async () => {
    fetchMock().mockReturnValue(ok({ default_engine: "vllm", engines: [] }));
    const data = await fetchEngines();
    expect(data.default_engine).toBe("vllm");
    const [url, init] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/engines");
    expect((init?.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("fetchEngine defaults the variant", async () => {
    fetchMock().mockReturnValue(ok({ engine: "vllm", variant: "default" }));
    await fetchEngine("vllm");
    expect(fetchMock().mock.calls[0][0]).toBe("/api/engines/vllm/default");
  });

  it("fetchEngine passes an explicit variant", async () => {
    fetchMock().mockReturnValue(ok({ engine: "vllm", variant: "b12x" }));
    await fetchEngine("vllm", "b12x");
    expect(fetchMock().mock.calls[0][0]).toBe("/api/engines/vllm/b12x");
  });

  it("refreshEngines POSTs /api/engines/refresh", async () => {
    fetchMock().mockReturnValue(ok({ refreshed: true, engines: 2, indexes: [] }));
    const result = await refreshEngines();
    expect(result.refreshed).toBe(true);
    const [url, init] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/engines/refresh");
    expect(init?.method).toBe("POST");
  });

  it("renderLaunch POSTs the request body", async () => {
    fetchMock().mockReturnValue(ok({ engine: "vllm", ranks: [] }));
    await renderLaunch({
      recipe_id: "qwen3-8b",
      engine: "sglang",
      params: { port: 9000 },
      extra_args: ["--trust"],
      nodes: ["spark-a", "spark-b"],
    });
    const [url, init] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/engines/render");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      recipe_id: "qwen3-8b",
      engine: "sglang",
      params: { port: 9000 },
      extra_args: ["--trust"],
      nodes: ["spark-a", "spark-b"],
    });
  });

  it("throws on a non-ok response", async () => {
    fetchMock().mockReturnValue(
      Promise.resolve({
        ok: false,
        status: 404,
        text: () => Promise.resolve("not found"),
      } as Response),
    );
    await expect(fetchEngine("nope")).rejects.toThrow("API 404");
  });
});
