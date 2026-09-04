import { useState } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DeployOptions, {
  deployParams,
  describeImagePresence,
  describeOccupancy,
  eligibleEngines,
  engineChoices,
  engineLabel,
  occupancy,
  parseExtraArgs,
  proposeParallelism,
  recipeParallelism,
  type DeployOptionsValue,
} from "@/components/DeployOptions";
import type { ClusterNode, EngineSummary, RecipeDetail } from "@/lib/types";
import { MULTI_NODE_TITLE, MULTI_NODE_UNPROVEN } from "@/lib/experimental";

vi.mock("@/lib/api", () => ({
  fetchEngines: vi.fn(),
  fetchModels: vi.fn(),
  fetchNodes: vi.fn(),
  planDeployment: vi.fn(),
  runPreflight: vi.fn(),
}));

import { fetchEngines, fetchModels, fetchNodes, planDeployment, runPreflight } from "@/lib/api";

const node = (name: string, address: string, extra: Partial<ClusterNode> = {}): ClusterNode => ({
  id: `node-${address}`,
  name,
  address,
  is_control_plane: false,
  ssh_user: "",
  ssh_key_path: "",
  ethernet_interface: "",
  infiniband_interfaces: [],
  state: "healthy",
  last_seen: null,
  machine_id: "",
  ...extra,
});

const CONTROL_NODE = node("spark-01", "192.168.1.100", { id: "control-1", is_control_plane: true });
const PEER_NODE = node("spark-02", "10.0.0.11", { id: "peer-1" });
const PEER_NODE_2 = node("spark-03", "10.0.0.12", { id: "peer-2" });

/** A `DeployOptions` wired to real state, the way `RecipeDrawer` wires it —
 *  needed for anything that checks the value the control produces feeds back
 *  into what the control itself renders (the world size, node checkboxes). */
function ControlledDeployOptions({ recipe }: { recipe: RecipeDetail }) {
  const [value, setValue] = useState<DeployOptionsValue>({});
  return <DeployOptions recipe={recipe} value={value} onChange={setValue} />;
}

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

/** A recipe that already declares a shape occupying two nodes. */
const TP2_RECIPE = {
  ...V2_RECIPE,
  id: "tp2",
  defaults: { tensor_parallel: 2, pipeline_parallel: 1 },
  params: { tensor_parallel: 2, pipeline_parallel: 1 },
} as unknown as RecipeDetail;

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

/** A pre-flight that found one thing worth saying, on a named node. */
const CHECK = {
  id: "image",
  title: "Engine image",
  node: "spark-02",
  node_id: "peer-1",
  status: "warn",
  observed: "ghcr.io/example/vllm:0.1.0 is not on spark-02",
  remedy: "Pre-seed it with POST /api/images/sync.",
  delay_bytes: 26_843_545_600,
  costs_time: true,
  detail: {},
};

const REPORT = {
  verdict: "slow",
  summary: "will run, but about 25.0 GB has to transfer first across spark-02",
  can_proceed: true,
  delays: true,
  estimated_transfer_bytes: 26_843_545_600,
  counts: { pass: 8, warn: 1, fail: 0 },
  nodes: [
    { id: "control-1", label: "spark-01", address: "", is_control_plane: true, ranks: [0] },
    { id: "peer-1", label: "spark-02", address: "10.0.0.11", is_control_plane: false, ranks: [1] },
  ],
  checks: [CHECK],
  blocking: [],
  delaying: [CHECK],
  advisories: [],
  plan: {
    recipe_id: "qwen3-8b",
    engine: "vllm",
    variant: "default",
    image_ref: "ghcr.io/example/vllm:0.1.0",
    model: "Qwen/Qwen3-8B",
    port: 9000,
    rendezvous_port: null,
    node_count: 2,
  },
  checked_at: "2026-01-01T00:00:00+00:00",
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
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE]);
    vi.mocked(planDeployment).mockResolvedValue(PLAN as never);
    vi.mocked(runPreflight).mockResolvedValue(REPORT as never);
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
    // engine/variant, not the bare engine name: the backend splits on "/",
    // and two variants of one engine are two different images.
    await user.selectOptions(screen.getByLabelText("Engine"), "sglang/default");
    expect(onChange).toHaveBeenCalledWith({ engine: "sglang/default" });

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
      nodes: undefined,
      // The plan is asked about the shape the form is showing, always — a
      // preview of some other parallelism is a preview of another deployment.
      params: { tensor_parallel: 1, pipeline_parallel: 1 },
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

describe("describeImagePresence", () => {
  it("says how much will download when the image is absent", () => {
    expect(describeImagePresence({ image_present: false, image_size_bytes: null })).toBe(
      "image not pulled, several GB will download first",
    );
  });

  it("quotes the known size when the plan has one", () => {
    expect(
      describeImagePresence({ image_present: false, image_size_bytes: 26_843_545_600 }),
    ).toBe("image not pulled, 25.0 GB will download first");
  });

  it("says the image is pulled, with its size", () => {
    expect(
      describeImagePresence({ image_present: true, image_size_bytes: 26_843_545_600 }),
    ).toBe("pulled · 25.0 GB");
  });
});

describe("DeployOptions image presence", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchEngines).mockResolvedValue({ engines: [engine("vllm")] } as never);
    vi.mocked(fetchModels).mockResolvedValue([] as never);
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE]);
  });

  it("warns in the preview when the deploy would have to pull first", async () => {
    vi.mocked(planDeployment).mockResolvedValue({
      ...PLAN,
      image_present: false,
      image_size_bytes: 26_843_545_600,
    } as never);
    render(<DeployOptions recipe={V1_RECIPE} value={{ engine: "vllm" }} onChange={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() =>
      expect(
        screen.getByText("image not pulled, 25.0 GB will download first"),
      ).toBeInTheDocument(),
    );
  });

  it("says nothing alarming when the image is already there", async () => {
    vi.mocked(planDeployment).mockResolvedValue({
      ...PLAN,
      image_present: true,
      image_size_bytes: 26_843_545_600,
    } as never);
    render(<DeployOptions recipe={V1_RECIPE} value={{ engine: "vllm" }} onChange={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() => expect(screen.getByText("pulled · 25.0 GB")).toBeInTheDocument());
    expect(screen.queryByText(/will download first/)).not.toBeInTheDocument();
  });

});

/** The preview is where an operator decides, so it is where the pre-flight goes. */
describe("DeployOptions pre-flight", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchEngines).mockResolvedValue({ engines: [engine("vllm")] } as never);
    vi.mocked(fetchModels).mockResolvedValue([] as never);
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE]);
    vi.mocked(planDeployment).mockResolvedValue(PLAN as never);
    vi.mocked(runPreflight).mockResolvedValue(REPORT as never);
  });

  const previewed = async () => {
    render(<DeployOptions recipe={V1_RECIPE} value={{ engine: "vllm" }} onChange={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));
    await user.click(screen.getByRole("button", { name: /preview/i }));
    return user;
  };

  it("asks the pre-flight the same question it asked the plan", async () => {
    await previewed();

    await waitFor(() => expect(screen.getByTestId("preflight")).toBeInTheDocument());
    expect(vi.mocked(runPreflight)).toHaveBeenCalledWith(
      vi.mocked(planDeployment).mock.calls[0][0],
    );
  });

  it("shows the verdict and names the node and the remedy", async () => {
    await previewed();

    await waitFor(() => expect(screen.getByTestId("preflight")).toBeInTheDocument());
    expect(screen.getByTestId("preflight-verdict")).toHaveTextContent("Ready, but slow");
    expect(screen.getByText(/is not on spark-02/)).toBeInTheDocument();
    expect(screen.getByText(/Pre-seed it with/)).toBeInTheDocument();
  });

  it("still shows the plan when the pre-flight itself fails", async () => {
    vi.mocked(runPreflight).mockRejectedValue(new Error("preflight exploded"));
    await previewed();

    await waitFor(() => expect(screen.getByTestId("deploy-plan")).toBeInTheDocument());
    expect(screen.queryByTestId("preflight")).not.toBeInTheDocument();
  });
});

/** A single-node registry has exactly one possible answer to "which nodes take
 *  part" — the control node, alone — so there is nothing for a selector to
 *  offer, and one should not appear just because the feature exists. */
describe("the parallelism a recipe declares", () => {
  it("is 1x1 when the recipe says nothing", () => {
    expect(recipeParallelism(V1_RECIPE)).toEqual({ tensor_parallel: 1, pipeline_parallel: 1 });
  });

  it("is what the recipe declares", () => {
    expect(recipeParallelism(TP2_RECIPE)).toEqual({ tensor_parallel: 2, pipeline_parallel: 1 });
  });

  it("reads `params`, which is the name the v2 schema uses", () => {
    const recipe = {
      ...V2_RECIPE,
      defaults: {},
      params: { tensor_parallel: 4, pipeline_parallel: 2 },
    } as unknown as RecipeDetail;
    expect(recipeParallelism(recipe)).toEqual({ tensor_parallel: 4, pipeline_parallel: 2 });
  });

  /** Recipes are YAML an operator can hand-edit, so the value can be a string
   *  or nonsense. Neither should put NaN into a request body. */
  it("takes a numeric string, and refuses anything that is not a count", () => {
    const stringy = { ...V2_RECIPE, params: { tensor_parallel: "2" } } as unknown as RecipeDetail;
    expect(recipeParallelism(stringy).tensor_parallel).toBe(2);
    const junk = {
      ...V2_RECIPE,
      params: { tensor_parallel: "many", pipeline_parallel: 0 },
    } as unknown as RecipeDetail;
    expect(recipeParallelism(junk)).toEqual({ tensor_parallel: 1, pipeline_parallel: 1 });
  });
});

describe("proposeParallelism", () => {
  it("leaves a shape that already fits alone", () => {
    const fits = { tensor_parallel: 2, pipeline_parallel: 1 };
    expect(proposeParallelism(2, fits)).toBe(fits);
  });

  /** The whole point: ticking a second node must leave a form that deploys. */
  it("widens tensor parallelism to the node count", () => {
    expect(proposeParallelism(2, { tensor_parallel: 1, pipeline_parallel: 1 })).toEqual({
      tensor_parallel: 2,
      pipeline_parallel: 1,
    });
    expect(proposeParallelism(4, { tensor_parallel: 1, pipeline_parallel: 1 })).toEqual({
      tensor_parallel: 4,
      pipeline_parallel: 1,
    });
  });

  it("comes back down when peers are unticked", () => {
    expect(proposeParallelism(1, { tensor_parallel: 4, pipeline_parallel: 1 })).toEqual({
      tensor_parallel: 1,
      pipeline_parallel: 1,
    });
  });

  /** A pipeline depth is a property of the model's layers, not of how many
   *  machines happen to be ticked, so it survives when it still divides. */
  it("keeps a pipeline depth the node count divides by", () => {
    expect(proposeParallelism(4, { tensor_parallel: 1, pipeline_parallel: 2 })).toEqual({
      tensor_parallel: 2,
      pipeline_parallel: 2,
    });
  });

  it("falls back to a flat shape when the depth does not divide", () => {
    expect(proposeParallelism(3, { tensor_parallel: 1, pipeline_parallel: 2 })).toEqual({
      tensor_parallel: 3,
      pipeline_parallel: 1,
    });
  });

  it("counts the product as the nodes occupied", () => {
    expect(occupancy({ tensor_parallel: 2, pipeline_parallel: 3 })).toBe(6);
  });
});

describe("describeOccupancy", () => {
  it("says a matching shape occupies exactly the nodes selected", () => {
    const fit = describeOccupancy({ tensor_parallel: 2, pipeline_parallel: 1 }, 2);
    expect(fit.fits).toBe(true);
    expect(fit.text).toContain("tp=2 pp=1");
    expect(fit.text).toContain("occupies 2 nodes");
  });

  it("uses the singular for a solo deployment", () => {
    expect(describeOccupancy({ tensor_parallel: 1, pipeline_parallel: 1 }, 1).text).toContain(
      "occupies 1 node —",
    );
  });

  /** The words are the server's on purpose: an operator who ignores this line
   *  meets `_check_capacity` next, and two wordings read as two rules. */
  it("says what the server would say when the shape is too small", () => {
    const fit = describeOccupancy({ tensor_parallel: 1, pipeline_parallel: 1 }, 2);
    expect(fit.fits).toBe(false);
    expect(fit.text).toContain("only occupies 1 of the 2 nodes selected");
    expect(fit.text).toContain("nothing to hold");
    expect(fit.text).toContain("the launch would hang");
    expect(fit.text).toContain("raise the parallelism until tp*pp is 2");
  });

  it("says what the server would say when the shape is too large", () => {
    const fit = describeOccupancy({ tensor_parallel: 4, pipeline_parallel: 1 }, 2);
    expect(fit.fits).toBe(false);
    expect(fit.text).toContain("does not fit 2 nodes: it needs 4");
    expect(fit.text).toContain("one GPU per node");
    expect(fit.text).toContain("deploy across 4 nodes");
  });
});

describe("deployParams", () => {
  it("sends the recipe's own shape when the operator has changed nothing", () => {
    expect(deployParams(TP2_RECIPE, {})).toEqual({ tensor_parallel: 2, pipeline_parallel: 1 });
  });

  it("sends what the operator set", () => {
    expect(deployParams(V1_RECIPE, { tensor_parallel: 4, pipeline_parallel: 2 })).toEqual({
      tensor_parallel: 4,
      pipeline_parallel: 2,
    });
  });
});

describe("DeployOptions parallelism", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchEngines).mockResolvedValue({ engines: [engine("vllm")] } as never);
    vi.mocked(fetchModels).mockResolvedValue([] as never);
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE, PEER_NODE, PEER_NODE_2]);
    vi.mocked(planDeployment).mockResolvedValue(PLAN as never);
    vi.mocked(runPreflight).mockResolvedValue(REPORT as never);
  });

  const openOptions = async () => {
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));
    await waitFor(() => expect(screen.getByTestId("deploy-parallelism")).toBeInTheDocument());
    return user;
  };

  it("seeds both fields from the recipe, so the number shown is the number sent", async () => {
    render(<ControlledDeployOptions recipe={TP2_RECIPE} />);
    await openOptions();

    expect(screen.getByLabelText("Tensor parallel")).toHaveValue(2);
    expect(screen.getByLabelText("Pipeline parallel")).toHaveValue(1);
  });

  it("seeds a recipe that declares nothing at 1", async () => {
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    await openOptions();

    expect(screen.getByLabelText("Tensor parallel")).toHaveValue(1);
  });

  /** The bug this control exists for: the node selector offered a topology the
   *  form could not express, so ticking a peer produced a request the server
   *  refuses and nothing on the page could raise the parallelism. */
  it("proposes the shape that fits when a peer is ticked", async () => {
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = await openOptions();

    expect(screen.getByTestId("deploy-occupancy")).toHaveTextContent("occupies 1 node");
    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));

    await waitFor(() => expect(screen.getByLabelText("Tensor parallel")).toHaveValue(2));
    expect(screen.getByTestId("deploy-world-size")).toHaveTextContent("2 nodes");
    expect(screen.getByTestId("deploy-occupancy")).toHaveTextContent("tp=2 pp=1 occupies 2 nodes");
  });

  it("follows the node count back down when the peer is unticked", async () => {
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = await openOptions();

    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));
    await waitFor(() => expect(screen.getByLabelText("Tensor parallel")).toHaveValue(2));
    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));

    await waitFor(() => expect(screen.getByLabelText("Tensor parallel")).toHaveValue(1));
    expect(screen.getByTestId("deploy-occupancy")).toHaveTextContent("occupies 1 node");
  });

  it("leaves a recipe that already fits two nodes alone", async () => {
    render(<ControlledDeployOptions recipe={TP2_RECIPE} />);
    const user = await openOptions();

    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));
    await waitFor(() => expect(screen.getByTestId("deploy-world-size")).toHaveTextContent("2 nodes"));
    expect(screen.getByLabelText("Tensor parallel")).toHaveValue(2);
  });

  /** A proposal is for a form still showing the recipe's number. Once an
   *  operator has said what they want, the node count must not overwrite it —
   *  it must show them the mismatch instead. */
  it("never overwrites a value the operator typed", async () => {
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = await openOptions();

    const tp = screen.getByLabelText("Tensor parallel");
    await user.clear(tp);
    await user.type(tp, "4");
    await waitFor(() => expect(tp).toHaveValue(4));

    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));
    await waitFor(() => expect(screen.getByTestId("deploy-world-size")).toHaveTextContent("2 nodes"));
    expect(tp).toHaveValue(4);
    // And the mismatch is on the page rather than waiting for a 400.
    expect(screen.getByTestId("deploy-occupancy")).toHaveTextContent("does not fit 2 nodes");
  });

  it("shows the mismatch live while a shape is being typed", async () => {
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = await openOptions();

    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));
    await waitFor(() =>
      expect(screen.getByTestId("deploy-occupancy")).toHaveTextContent("occupies 2 nodes"),
    );

    const tp = screen.getByLabelText("Tensor parallel");
    await user.clear(tp);
    await user.type(tp, "1");
    await waitFor(() =>
      expect(screen.getByTestId("deploy-occupancy")).toHaveTextContent("only occupies 1 of the 2"),
    );
    expect(screen.getByTestId("deploy-occupancy")).toHaveTextContent("the launch would hang");
  });

  /** Half-typed text is not an instruction. Clearing the field to retype must
   *  not snap the value back to a digit — that is how "2" becomes "12". */
  it("keeps the last whole number while the field is empty", async () => {
    const onChange = vi.fn();
    render(<DeployOptions recipe={TP2_RECIPE} value={{ tensor_parallel: 2 }} onChange={onChange} />);
    const user = await openOptions();

    onChange.mockClear();
    await user.clear(screen.getByLabelText("Tensor parallel"));
    expect(screen.getByLabelText("Tensor parallel")).toHaveValue(null);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("sends the shape to the plan and the pre-flight, and the same one to both", async () => {
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = await openOptions();

    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));
    await waitFor(() => expect(screen.getByLabelText("Tensor parallel")).toHaveValue(2));
    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() => expect(planDeployment).toHaveBeenCalled());
    const planned = vi.mocked(planDeployment).mock.calls[0][0];
    expect(planned.params).toEqual({ tensor_parallel: 2, pipeline_parallel: 1 });
    await waitFor(() => expect(runPreflight).toHaveBeenCalled());
    expect(vi.mocked(runPreflight).mock.calls[0][0].params).toEqual(planned.params);
  });

  it("sends a pipeline depth the operator set", async () => {
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = await openOptions();

    const pp = screen.getByLabelText("Pipeline parallel");
    await user.clear(pp);
    await user.type(pp, "2");
    await waitFor(() => expect(pp).toHaveValue(2));
    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() => expect(planDeployment).toHaveBeenCalled());
    expect(vi.mocked(planDeployment).mock.calls[0][0].params).toMatchObject({
      pipeline_parallel: 2,
    });
  });

  /** The registry can hold one node, or none, and the shape still matters:
   *  a tp of 2 on one node is refused just as loudly. */
  it("offers the control with no node selector at all", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE]);
    render(<ControlledDeployOptions recipe={TP2_RECIPE} />);
    await openOptions();

    expect(screen.queryByTestId("deploy-nodes")).not.toBeInTheDocument();
    expect(screen.getByTestId("deploy-occupancy")).toHaveTextContent("does not fit 1 node");
  });
});

describe("DeployOptions node selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchEngines).mockResolvedValue({ engines: [engine("vllm")] } as never);
    vi.mocked(fetchModels).mockResolvedValue([] as never);
    vi.mocked(planDeployment).mockResolvedValue(PLAN as never);
    vi.mocked(runPreflight).mockResolvedValue(REPORT as never);
  });

  it("shows no node control when the registry has only the control node", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE]);
    render(<DeployOptions recipe={V1_RECIPE} value={{}} onChange={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await waitFor(() => expect(screen.getByLabelText("Model override")).toBeInTheDocument());
    expect(screen.queryByTestId("deploy-nodes")).not.toBeInTheDocument();
  });

  it("shows no node control when the registry is empty", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([]);
    render(<DeployOptions recipe={V1_RECIPE} value={{}} onChange={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await waitFor(() => expect(screen.getByLabelText("Model override")).toBeInTheDocument());
    expect(screen.queryByTestId("deploy-nodes")).not.toBeInTheDocument();
  });

  it("lets the operator add nodes and reports the world size as ranks", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE, PEER_NODE, PEER_NODE_2]);
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await waitFor(() => expect(screen.getByTestId("deploy-nodes")).toBeInTheDocument());
    expect(screen.getByTestId("deploy-world-size")).toHaveTextContent("1 node, ranks 0-0");

    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));
    await waitFor(() =>
      expect(screen.getByTestId("deploy-world-size")).toHaveTextContent("2 nodes, ranks 0-1"),
    );

    await user.click(screen.getByLabelText(new RegExp(PEER_NODE_2.name)));
    await waitFor(() =>
      expect(screen.getByTestId("deploy-world-size")).toHaveTextContent("3 nodes, ranks 0-2"),
    );
  });

  it("marks the node selector experimental even before a peer is picked", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE, PEER_NODE]);
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await waitFor(() => expect(screen.getByTestId("deploy-nodes")).toBeInTheDocument());
    // The badge is on the label, so the warning reaches an operator who is
    // only looking at the control rather than one who has already used it.
    const selector = screen.getByTestId("deploy-node-selector");
    expect(selector).toHaveTextContent("exp");
    // The banner is not: solo is not experimental, and this is still solo.
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("names what is unproven once more than one node is selected", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE, PEER_NODE]);
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await waitFor(() => expect(screen.getByTestId("deploy-nodes")).toBeInTheDocument());
    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));

    const note = await screen.findByRole("note");
    expect(note).toHaveTextContent(MULTI_NODE_TITLE);
    // Named risks, not the word "experimental" on its own.
    expect(note).toHaveTextContent(/rendezvous forms across machines/i);
    // And both kinds of risk, labelled: a behaviour a source specifies and we
    // have not run reads very differently from one nobody documents.
    expect(note).toHaveTextContent(/Specified, unconfirmed —/);
    expect(note).toHaveTextContent(/Documented nowhere —/);
    expect(note.querySelectorAll("li")).toHaveLength(MULTI_NODE_UNPROVEN.length);
  });

  it("keeps the control node checked and unclickable", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE, PEER_NODE]);
    render(<DeployOptions recipe={V1_RECIPE} value={{}} onChange={vi.fn()} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await waitFor(() => expect(screen.getByTestId("deploy-nodes")).toBeInTheDocument());
    const controlCheckbox = screen.getByLabelText(new RegExp(CONTROL_NODE.name)) as HTMLInputElement;
    expect(controlCheckbox.checked).toBe(true);
    expect(controlCheckbox.disabled).toBe(true);
  });

  it("puts the control node first and sends only the selected addresses to the plan and pre-flight", async () => {
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE, PEER_NODE, PEER_NODE_2]);
    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await waitFor(() => expect(screen.getByTestId("deploy-nodes")).toBeInTheDocument());
    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));
    await user.click(screen.getByRole("button", { name: /preview/i }));

    await waitFor(() => expect(planDeployment).toHaveBeenCalled());
    expect(vi.mocked(planDeployment).mock.calls[0][0]).toMatchObject({
      nodes: [CONTROL_NODE.address, PEER_NODE.address],
    });
    await waitFor(() => expect(runPreflight).toHaveBeenCalled());
    expect(vi.mocked(runPreflight).mock.calls[0][0]).toMatchObject({
      nodes: [CONTROL_NODE.address, PEER_NODE.address],
    });
  });
});

describe("engine picker with two variants of one engine", () => {
  // Publishing vllm-b12x makes this real: two images with different kernels.
  // The picker used to render `value={engine.engine}`, so both options said
  // "vllm · 0.1.0" and both deployed the default variant.
  const vllm = {
    engine: "vllm",
    variant: "default",
    key: "vllm/default",
    version: "0.1.0",
  } as unknown as EngineSummary;
  const b12x = {
    engine: "vllm",
    variant: "b12x",
    key: "vllm/b12x",
    version: "0.1.0",
  } as unknown as EngineSummary;

  it("names the variant, and says nothing where there is none", () => {
    expect(engineLabel(vllm)).toBe("vllm · 0.1.0");
    expect(engineLabel(b12x)).toBe("vllm/b12x · 0.1.0");
  });

  it("gives each variant its own option value", () => {
    // registry.select() splits the value on "/", so engine/variant is the
    // wire form. A bare "vllm" for the b12x option deploys the other image.
    const options = [vllm, b12x].map((e) => ({ value: e.key, label: engineLabel(e) }));
    expect(options.map((o) => o.value)).toEqual(["vllm/default", "vllm/b12x"]);
    expect(new Set(options.map((o) => o.label)).size).toBe(2);
  });
});

describe("DeployOptions when the form's own lookups fail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(planDeployment).mockResolvedValue(PLAN as never);
    vi.mocked(runPreflight).mockResolvedValue(REPORT as never);
  });

  /** Three independent lookups fill this form. None of them is the form: a
   *  node registry that cannot be reached must not cost the operator the
   *  engine picker, the args field or the preview. */
  it("still deploys when the engine, model and node lookups all fail", async () => {
    vi.mocked(fetchEngines).mockRejectedValue(new Error("engines are down"));
    vi.mocked(fetchModels).mockRejectedValue(new Error("no catalogue"));
    vi.mocked(fetchNodes).mockRejectedValue(new Error("no registry"));

    render(<ControlledDeployOptions recipe={V1_RECIPE} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));

    await waitFor(() => expect(screen.getByLabelText("Model override")).toBeInTheDocument());
    // The engine picker keeps its one always-valid option rather than emptying.
    expect(screen.getByLabelText("Engine")).toHaveValue("");
    expect(screen.getByRole("option", { name: "Recipe default" })).toBeInTheDocument();
    // No registry means no topology to choose, which is what solo already is.
    expect(screen.queryByTestId("deploy-nodes")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /preview/i }));
    await waitFor(() => expect(screen.getByTestId("deploy-plan")).toBeInTheDocument());
  });

  /** Unticking the last peer has to clear `nodes` entirely, not leave `[]`
   *  or a one-element list behind: a solo deploy is the one that sends no
   *  nodes at all, and anything else is planned as a cluster of one. */
  it("returns to a solo deployment when the last peer is unticked", async () => {
    vi.mocked(fetchEngines).mockResolvedValue({ engines: [engine("vllm")] } as never);
    vi.mocked(fetchModels).mockResolvedValue([] as never);
    vi.mocked(fetchNodes).mockResolvedValue([CONTROL_NODE, PEER_NODE]);

    const onChange = vi.fn();
    render(<DeployOptions recipe={V1_RECIPE} value={{ nodes: [] }} onChange={onChange} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /deploy options/i }));
    await waitFor(() => expect(screen.getByTestId("deploy-nodes")).toBeInTheDocument());

    // Tick the peer, then untick it: the second call is the one that matters.
    await user.click(screen.getByLabelText(new RegExp(PEER_NODE.name)));
    expect(onChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ nodes: [CONTROL_NODE.address, PEER_NODE.address] }),
    );

    onChange.mockClear();
    render(
      <DeployOptions
        recipe={V1_RECIPE}
        value={{ nodes: [CONTROL_NODE.address, PEER_NODE.address] }}
        onChange={onChange}
      />,
    );
    const [, second] = screen.getAllByRole("button", { name: /deploy options/i });
    await user.click(second);
    await waitFor(() => expect(screen.getAllByTestId("deploy-nodes")).toHaveLength(2));
    const ticked = screen.getAllByLabelText(new RegExp(PEER_NODE.name))[1];
    await user.click(ticked);

    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ nodes: undefined }));
  });
});
