import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  createDeployment,
  fetchDeployment,
  fetchDeployments,
  planDeployment,
} from "@/lib/api";

const ok = (body: unknown) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);

const PLAN = {
  deployment_id: "abc123",
  recipe_id: "qwen3-8b",
  engine: "vllm",
  variant: "default",
  image_ref: "ghcr.io/example/vllm:0.1.0",
  model: "Qwen/Qwen3-8B",
  port: 8000,
  launch_command: "vllm serve Qwen/Qwen3-8B --port 8000",
  ranks: [],
  warnings: [],
  container: { privileged: true },
};

describe("deployment api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

  it("fetchDeployments GETs /api/deployments", async () => {
    fetchMock().mockReturnValue(ok([]));
    await fetchDeployments();
    const [url, init] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/deployments");
    expect((init?.method ?? "GET").toUpperCase()).toBe("GET");
  });

  it("planDeployment POSTs the dry run to /api/deployments/plan", async () => {
    fetchMock().mockReturnValue(ok(PLAN));

    const plan = await planDeployment({
      recipe_id: "qwen3-8b",
      engine: "vllm",
      model: "Qwen/Qwen3-8B",
      extra_args: ["--enable-prefix-caching"],
    });

    expect(plan.launch_command).toBe("vllm serve Qwen/Qwen3-8B --port 8000");
    expect(plan.image_ref).toBe("ghcr.io/example/vllm:0.1.0");

    const [url, init] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/deployments/plan");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      recipe_id: "qwen3-8b",
      engine: "vllm",
      model: "Qwen/Qwen3-8B",
      extra_args: ["--enable-prefix-caching"],
    });
  });

  it("createDeployment forwards engine, model and extra args", async () => {
    fetchMock().mockReturnValue(ok({ id: "abc123", runtime: "native" }));

    const deployment = await createDeployment({
      recipe_id: "qwen3-8b",
      name: "run",
      params: {},
      engine: "vllm",
      model: "Qwen/Qwen3-8B",
      extra_args: ["--max-num-seqs", "16"],
    });

    expect(deployment.runtime).toBe("native");

    const [url, init] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/deployments");
    expect(init?.method).toBe("POST");
    const body = JSON.parse(init?.body as string);
    expect(body.engine).toBe("vllm");
    expect(body.model).toBe("Qwen/Qwen3-8B");
    expect(body.extra_args).toEqual(["--max-num-seqs", "16"]);
  });

  it("createDeployment omits the native fields when they are not set", async () => {
    fetchMock().mockReturnValue(ok({ id: "abc123" }));
    await createDeployment({ recipe_id: "qwen3-8b", name: "run", params: {} });

    const body = JSON.parse(fetchMock().mock.calls[0][1]?.body as string);
    expect(body).toEqual({ recipe_id: "qwen3-8b", name: "run", params: {} });
  });

  it("fetchDeployment GETs one deployment", async () => {
    fetchMock().mockReturnValue(ok({ id: "abc123", ready: true }));
    const deployment = await fetchDeployment("abc123");
    expect(deployment.ready).toBe(true);
    expect(fetchMock().mock.calls[0][0]).toBe("/api/deployments/abc123");
  });

  it("surfaces the backend's explanation when a plan is refused", async () => {
    fetchMock().mockReturnValue(
      Promise.resolve({
        ok: false,
        status: 400,
        text: () => Promise.resolve("engine 'sglang/default' cannot run this recipe"),
      } as Response),
    );

    await expect(planDeployment({ recipe_id: "qwen3-8b", engine: "sglang" })).rejects.toThrow(
      /cannot run this recipe/,
    );
  });
});
