import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DeployOptions, {
  eligibleEngines,
  engineChoices,
  parseExtraArgs,
} from "@/components/DeployOptions";
import type { EngineSummary, RecipeDetail } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchEngines: vi.fn(),
  fetchModels: vi.fn(),
  planDeployment: vi.fn(),
}));

import { fetchEngines, fetchModels, planDeployment } from "@/lib/api";

const engine = (name: string, extra: Partial<EngineSummary> = {}): EngineSummary =>
  ({
    engine: name,
    variant: "default",
    key: `${name}/default`,
    description: "",
    image: `ghcr.io/example/${name}`,
    image_ref: `ghcr.io/example/${name}:0.1.0`,
    version: "0.1.0",
    tag: "0.1.0",
    digest: null,
    legacy_tags: [],
    capabilities: { mods: false, pr_mods: false, solo: true, cluster: false, mesh: false },
    verified: [],
    ports: { api: 8000, rendezvous: null },
    readiness: "/v1/models",
    metrics: "/metrics",
    source: "bundled",
    enabled: true,
    ...extra,
  }) as EngineSummary;

const V1_RECIPE = {
  id: "qwen3-8b",
  name: "Qwen3 8B",
  model: "Qwen/Qwen3-8B",
  container: "vllm-node",
  command: "vllm serve Qwen/Qwen3-8B --port {port}",
  description: "",
  mods: [],
  defaults: {},
  env: {},
  build_args: [],
  solo_only: false,
  cluster_only: false,
} as unknown as RecipeDetail;

const V2_RECIPE = { ...V1_RECIPE, id: "generic", command: "" } as RecipeDetail;

/** A bundled v2 recipe: the API reports a verdict per engine. */
const DUAL_ENGINE_RECIPE = {
  ...V2_RECIPE,
  id: "bundled/qwen2.5-0.5b-instruct",
  engines: ["vllm", "sglang"],
  engine_support: [
    { engine: "sglang", supported: true, reason: "", enabled: true },
    { engine: "vllm", supported: true, reason: "", enabled: true },
  ],
} as unknown as RecipeDetail;

/** A v1 recipe: the API explains why SGLang cannot run it. */
const REPORTED_V1_RECIPE = {
  ...V1_RECIPE,
  engines: ["vllm"],
  engine_support: [
    {
      engine: "sglang",
      supported: false,
      reason: "recipe carries an engine-specific command for 'vllm'",
      enabled: true,
    },
    { engine: "vllm", supported: true, reason: "", enabled: true },
  ],
} as unknown as RecipeDetail;

const PLAN = {
  deployment_id: "abc123",
  recipe_id: "qwen3-8b",
  recipe_name: "Qwen3 8B",
  name: "Qwen3 8B",
  engine: "vllm",
  variant: "default",
  image_ref: "ghcr.io/example/vllm:0.1.0",
  model: "Qwen/Qwen3-8B",
  solo: true,
  nodes: [],
  node_count: 1,
  port: 9000,
  rendezvous_port: null,
  readiness_path: "/v1/models",
  readiness_url: "http://127.0.0.1:9000/v1/models",
  metrics_path: "/metrics",
  mods: ["fix-qwen"],
  params: {},
  extra_args: [],
  launch_command: "vllm serve Qwen/Qwen3-8B --port 9000",
  ranks: [],
  container: {
    privileged: true,
    network_host: true,
    ipc_host: true,
    shm_size_gb: 64,
  },
  cache_mounts: [],
  warnings: ["recipe container tag 'vllm-node-mxfp4' is not claimed by any engine"],
  runtime: "native",
  created_at: "2026-01-01T00:00:00+00:00",
};

describe("parseExtraArgs", () => {
  it("splits on whitespace", () => {
    expect(parseExtraArgs("--a 1 --b")).toEqual(["--a", "1", "--b"]);
  });

  it("keeps quoted values together", () => {
    expect(parseExtraArgs('--chat-template "my template.jinja"')).toEqual([
      "--chat-template",
      "my template.jinja",
    ]);
  });

  it("is empty for blank input", () => {
    expect(parseExtraArgs("   ")).toEqual([]);
  });
});

describe("eligibleEngines", () => {
  it("limits a v1 command recipe to vLLM", () => {
    const engines = [engine("vllm"), engine("sglang")];
    expect(eligibleEngines(engines, V1_RECIPE).map((e) => e.engine)).toEqual(["vllm"]);
  });

  it("offers every enabled engine for a recipe without a command", () => {
    const engines = [engine("vllm"), engine("sglang")];
    expect(eligibleEngines(engines, V2_RECIPE).map((e) => e.engine)).toEqual(["vllm", "sglang"]);
  });

  it("hides disabled engines", () => {
    const engines = [engine("vllm"), engine("sglang", { enabled: false })];
    expect(eligibleEngines(engines, V2_RECIPE).map((e) => e.engine)).toEqual(["vllm"]);
  });

  it("follows the API's per-engine verdict over the command heuristic", () => {
    const engines = [engine("vllm"), engine("sglang")];
    expect(eligibleEngines(engines, DUAL_ENGINE_RECIPE).map((e) => e.engine)).toEqual([
      "vllm",
      "sglang",
    ]);
  });

  it("drops an engine the API reports as unsupported, keeping its reason", () => {
    const engines = [engine("vllm"), engine("sglang")];
    const choices = engineChoices(engines, REPORTED_V1_RECIPE);

    expect(choices.filter((c) => c.supported).map((c) => c.engine.engine)).toEqual(["vllm"]);
    const refused = choices.find((c) => c.engine.engine === "sglang");
    expect(refused?.supported).toBe(false);
    expect(refused?.reason).toContain("engine-specific command");
  });

  it("falls back to the command heuristic when no verdict is reported", () => {
    const engines = [engine("vllm"), engine("sglang")];
    const refused = engineChoices(engines, V1_RECIPE).find((c) => c.engine.engine === "sglang");
    expect(refused?.supported).toBe(false);
    expect(refused?.reason).toContain("engine-specific command");
  });
});

describe("DeployOptions", () => {
  beforeEach(() => {
    vi.mocked(fetchEngines).mockResolvedValue({
      default_engine: "vllm",
      engines: [engine("vllm"), engine("sglang")],
    });
    vi.mocked(fetchModels).mockResolvedValue([
      { id: "Qwen/Qwen3-8B" },
      { id: "openai/gpt-oss-120b" },
    ] as never);
    vi.mocked(planDeployment).mockResolvedValue(PLAN as never);
  });

  const open = async () => {
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));
    return user;
  };

  it("offers only the engines the recipe can run", async () => {
    render(<DeployOptions recipe={V1_RECIPE} value={{}} onChange={vi.fn()} />);
    await open();

    await waitFor(() => expect(screen.getByLabelText("Engine")).toBeInTheDocument());
    const options = Array.from(
      screen.getByLabelText("Engine").querySelectorAll("option"),
    ).map((o) => o.textContent);
    expect(options).toEqual(["Recipe default", "vllm · 0.1.0"]);
  });

  it("says why an engine is unavailable", async () => {
    render(<DeployOptions recipe={REPORTED_V1_RECIPE} value={{}} onChange={vi.fn()} />);
    await open();

    await waitFor(() => expect(screen.getByTestId("engines-unavailable")).toBeInTheDocument());
    expect(screen.getByText(/unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/engine-specific command/)).toBeInTheDocument();
  });

  it("offers both engines for a recipe that declares both", async () => {
    render(<DeployOptions recipe={DUAL_ENGINE_RECIPE} value={{}} onChange={vi.fn()} />);
    await open();

    await waitFor(() => expect(screen.getByLabelText("Engine")).toBeInTheDocument());
    const options = Array.from(screen.getByLabelText("Engine").querySelectorAll("option")).map(
      (o) => o.textContent,
    );
    expect(options).toEqual(["Recipe default", "vllm · 0.1.0", "sglang · 0.1.0"]);
    expect(screen.queryByTestId("engines-unavailable")).not.toBeInTheDocument();
  });

  it("reports the chosen engine and model override", async () => {
    const onChange = vi.fn();
    render(<DeployOptions recipe={V2_RECIPE} value={{}} onChange={onChange} />);
    const user = await open();

    await waitFor(() => expect(screen.getByLabelText("Engine")).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("Engine"), "sglang");
    expect(onChange).toHaveBeenCalledWith({ engine: "sglang" });

    onChange.mockClear();
    await user.type(screen.getByLabelText("Model override"), "o");
    expect(onChange).toHaveBeenLastCalledWith({ model: "o" });
  });

  it("parses extra args into argv", async () => {
    const onChange = vi.fn();
    render(<DeployOptions recipe={V2_RECIPE} value={{}} onChange={onChange} />);
    const user = await open();

    await user.type(screen.getByLabelText("Extra args"), "--x");
    expect(onChange).toHaveBeenLastCalledWith({ extra_args: ["--x"] });
  });

  it("previews the plan: command, image, model and warnings", async () => {
    render(<DeployOptions recipe={V1_RECIPE} value={{ engine: "vllm" }} onChange={vi.fn()} />);
    const user = await open();

    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() => expect(screen.getByTestId("deploy-plan")).toBeInTheDocument());
    expect(screen.getByText("vllm serve Qwen/Qwen3-8B --port 9000")).toBeInTheDocument();
    expect(screen.getByText("ghcr.io/example/vllm:0.1.0")).toBeInTheDocument();
    expect(screen.getByText("Qwen/Qwen3-8B")).toBeInTheDocument();
    expect(screen.getByText("fix-qwen")).toBeInTheDocument();
    expect(screen.getByText(/is not claimed by any engine/)).toBeInTheDocument();
    expect(screen.getByText(/privileged · network host/)).toBeInTheDocument();

    expect(vi.mocked(planDeployment)).toHaveBeenCalledWith({
      recipe_id: "qwen3-8b",
      engine: "vllm",
      model: undefined,
      extra_args: [],
    });
  });

  it("shows why a plan was refused", async () => {
    vi.mocked(planDeployment).mockRejectedValue(
      new Error("engine 'sglang/default' cannot run this recipe"),
    );
    render(<DeployOptions recipe={V1_RECIPE} value={{ engine: "sglang" }} onChange={vi.fn()} />);
    const user = await open();

    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() =>
      expect(screen.getByText(/cannot run this recipe/)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("deploy-plan")).not.toBeInTheDocument();
  });
});
