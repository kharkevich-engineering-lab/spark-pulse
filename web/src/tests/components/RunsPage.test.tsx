/** Runs: one list, three pills.
 *
 * What a row says about itself (where its ranks are, how it is doing, how long
 * it has been up), which pill it lands under, and the three different things
 * the teardown button means — plus everything the expanded row carried before
 * the page was one page: the log stream, the per-rank read, the engine's own
 * metrics window and the deployment event stream.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import RunsPage from "@/pages/RunsPage";
import RunDetailPage from "@/pages/RunDetailPage";
import { FUN_FACT_TOPICS, curlExample, shellQuote } from "@/components/RunRow";
import { translatorFor } from "@/lib/i18n";
import type { BenchmarkResult, Deployment } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  // Inert by default: only the tests that care about rank state stub it.
  fetchDeployment: vi.fn(() => Promise.resolve(undefined)),
  fetchDeployments: vi.fn(),
  // Also inert by default: the metrics panel is exercised in its own file and
  // in the tests below that stub this deliberately.
  fetchEngineMetrics: vi.fn(() => Promise.resolve(undefined)),
  // An empty history by default — every run that has not had one stubbed has
  // nothing stored, which is what the old behaviour looked like.
  fetchDeploymentEvents: vi.fn(() =>
    Promise.resolve({ resource: "", events: [], total: 0, limit: 200 }),
  ),
  stopDeployment: vi.fn(),
  connectLogStream: vi.fn(() => () => {}),
  runBenchmark: vi.fn(),
  fetchBenchmarks: vi.fn(() => Promise.resolve([])),
  fetchLatestByRecipe: vi.fn(() => Promise.resolve({})),
  compareRuns: vi.fn(),
  deleteBenchmark: vi.fn(),
  // Nothing waiting on a download unless a test says so.
  fetchScheduledDeploys: vi.fn(() => Promise.resolve([])),
  fetchModelDownloads: vi.fn(() => Promise.resolve([])),
  cancelScheduledDeploy: vi.fn(() => Promise.resolve({})),
}));

/** The feature flag, readable by the tests that turn it off. */
const appConfig = vi.hoisted(
  () =>
    ({ benchmarking_enabled: true }) as {
      benchmarking_enabled: boolean;
      benchmarking_tool?: { available: boolean; reason: string; install: string };
    },
);
vi.mock("@/lib/config", () => ({
  useConfig: () => ({ config: appConfig, configLoaded: true }),
}));

import {
  cancelScheduledDeploy,
  connectLogStream,
  fetchBenchmarks,
  fetchModelDownloads,
  fetchScheduledDeploys,
  fetchDeployments,
  runBenchmark,
  stopDeployment,
} from "@/lib/api";

function deployment(over: Partial<Deployment> = {}): Deployment {
  return {
    id: "solo",
    recipe_id: "qwen3-8b",
    name: "solo job",
    params: {},
    nodes: null,
    status: "running",
    pid: null,
    port: 9000,
    created_at: "2026-01-01T00:00:00+00:00",
    started_at: null,
    stopped_at: null,
    error_message: null,
    node_count: 1,
    ...over,
  };
}

function benchmark(over: Partial<BenchmarkResult> = {}): BenchmarkResult {
  return {
    benchmark_id: "b1",
    deployment_id: "solo",
    recipe_id: "qwen3-8b",
    recipe_name: "Qwen3 8B",
    baseline_id: null,
    status: "completed",
    started_at: "2026-01-02T00:00:00+00:00",
    completed_at: "2026-01-02T00:01:00+00:00",
    params: {},
    results: { throughput: 45.2, latency_ms: 12.3 },
    ...over,
  };
}

const SOLO = deployment();
const GANG = deployment({
  id: "gang",
  name: "gang job",
  node_count: 2,
  nodes: ["10.0.0.10", "10.0.0.11"],
  params: { tensor_parallel: 2 },
});
/** A record written before the field existed. */
const LEGACY = deployment({ id: "legacy", name: "legacy job", node_count: undefined });

function show(path = "/jobs") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/jobs" element={<RunsPage />} />
        <Route path="/jobs/:id" element={<RunDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** Move to a pill by its label — the counts are part of the accessible name. */
async function openTab(label: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name: new RegExp(`^${label}`) }));
}

beforeEach(() => {
  appConfig.benchmarking_enabled = true;
  appConfig.benchmarking_tool = undefined;
});

/** "Download and deploy" from the lab: the deploy waits on a 5 GB download,
 *  and until it lands there is no run. Runs said nothing, so it looked like
 *  nothing had happened. */
describe("RunsPage — a deploy waiting on its model", () => {
  const ENTRY = {
    id: "s-1",
    model: "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
    download_job_id: "job-1",
    status: "waiting",
    name: "qwen gguf",
    recipe_id: "custom-qwen",
    request: {},
    deployment_id: "",
    error: "",
    created_at: "",
    finished_at: "",
  };
  const JOB = {
    id: "job-1",
    model: ENTRY.model,
    source: "hf",
    revision: null,
    allow_patterns: null,
    status: "running",
    bytes_done: 2 * 1024 ** 3,
    bytes_total: 5 * 1024 ** 3,
    current_file: null,
    path: null,
    error: null,
    created_at: "",
    started_at: null,
    finished_at: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchDeployments).mockResolvedValue([]);
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([ENTRY] as never);
    vi.mocked(fetchModelDownloads).mockResolvedValue([JOB] as never);
  });

  // `clearAllMocks` keeps implementations, so put back "nothing waiting" for
  // every describe after this one.
  afterEach(() => {
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([]);
    vi.mocked(fetchModelDownloads).mockResolvedValue([]);
  });

  it("is on Live at once, with the download's progress and where to watch it", async () => {
    show();

    const row = await screen.findByTestId("waiting-s-1");
    expect(row).toHaveTextContent("qwen gguf");
    expect(row).toHaveTextContent("Waiting for model");
    expect(screen.getByTestId("waiting-progress-s-1")).toHaveTextContent(/Downloading the model: 40%/);
    expect(within(row).getByRole("link", { name: "watch the download" })).toHaveAttribute("href", "/models");
    expect(within(row).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40");
    expect(screen.getByRole("tab", { name: /^Live \(1\)/ })).toHaveAttribute("aria-selected", "true");
    // Not "no runs yet": something is happening.
    expect(screen.queryByText(/Nothing is running/)).toBeNull();
  });

  it("can be called off from the row", async () => {
    show();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Cancel the deploy of qwen gguf" }));

    expect(cancelScheduledDeploy).toHaveBeenCalledWith("s-1");
  });

  it("says the run is being created once the model has landed", async () => {
    vi.mocked(fetchScheduledDeploys).mockResolvedValue([{ ...ENTRY, status: "deploying" }] as never);
    show();

    expect(await screen.findByTestId("waiting-progress-s-1")).toHaveTextContent(
      "The model is here; the run is being created",
    );
    expect(screen.queryByRole("button", { name: /Cancel the deploy/ })).toBeNull();
  });
});

describe("RunsPage tabs", () => {
  const RUNNING = deployment({ id: "r1", name: "running job", status: "running" });
  const FINISHED = deployment({ id: "f1", name: "finished job", status: "stopped" });
  const ERRORED = deployment({ id: "e1", name: "errored job", status: "error" });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchDeployments).mockResolvedValue([RUNNING, FINISHED, ERRORED]);
  });

  it("counts what is live and what is finished, and opens on the live ones", async () => {
    show();

    expect(await screen.findByRole("tab", { name: /^Live \(1\)/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /^Finished \(2\)/ })).toBeInTheDocument();
    expect(screen.getByTestId("deployment-r1")).toBeInTheDocument();
    expect(screen.queryByTestId("deployment-f1")).toBeNull();
  });

  it("puts a stopped run and a failed one under Finished", async () => {
    show();
    await openTab("Finished");

    expect(screen.getByTestId("deployment-f1")).toBeInTheDocument();
    expect(screen.getByTestId("deployment-e1")).toBeInTheDocument();
    expect(screen.queryByTestId("deployment-r1")).toBeNull();
  });

  /** `?run=<id>` is how an older link points at one run; the run has a page
   *  of its own now, and the link lands on it. */
  it("sends an old link to one run to that run's page", async () => {
    show("/jobs?run=f1");
    expect(await screen.findByTestId("run-detail-f1")).toBeInTheDocument();
    await waitFor(() => expect(connectLogStream).toHaveBeenCalledWith("f1", expect.any(Function)));
  });

  /** Landing on an empty Live tab with a history sitting behind Finished is a
   *  page that looks broken to somebody who has stopped everything. */
  it("opens on the history when nothing is live", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([FINISHED]);
    show();

    expect(await screen.findByTestId("deployment-f1")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^Finished/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("offers the benchmarks tab only when the feature is on", async () => {
    show();
    expect(await screen.findByRole("tab", { name: /^Benchmarks/ })).toBeInTheDocument();
  });

  it("hides the benchmarks tab, and the launcher, when the feature is off", async () => {
    appConfig.benchmarking_enabled = false;
    show();

    await screen.findByTestId("deployment-r1");
    expect(screen.queryByRole("tab", { name: /^Benchmarks/ })).toBeNull();
    expect(fetchBenchmarks).not.toHaveBeenCalled();
  });

  /** Off, the button stays and the dialog says why nothing can start, and
   *  where to turn it on — a button that vanished explained nothing. */
  it("keeps the button when the feature is off, and the dialog says where to turn it on", async () => {
    appConfig.benchmarking_enabled = false;
    show();

    const row = await screen.findByTestId("deployment-r1");
    await userEvent.setup().click(within(row).getByRole("button", { name: "Benchmark" }));

    const notice = await screen.findByTestId("benchmarking-off");
    expect(notice).toHaveTextContent("Benchmarking is off");
    expect(within(notice).getByRole("link", { name: "Turn it on in Settings → Features" })).toHaveAttribute(
      "href",
      "/settings#features",
    );
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /Start|Run/ })).toBeDisabled();
  });

  /** The lab: llama-benchy was not installed. The click is told what to
   *  install, and nothing starts that would fail out of sight. */
  it("says what to install when the benchmark tool is missing", async () => {
    appConfig.benchmarking_tool = {
      available: false,
      reason: "llama-benchy is not installed on the control plane",
      install: "pip install 'spark-pulse[benchmarking]'",
    };
    show();

    const row = await screen.findByTestId("deployment-r1");
    await userEvent.setup().click(within(row).getByRole("button", { name: "Benchmark" }));

    const notice = await screen.findByTestId("benchmarking-missing");
    expect(notice).toHaveTextContent("llama-benchy");
    expect(
      within(notice).getByRole("button", { name: "Copy the install command pip install 'spark-pulse[benchmarking]'" }),
    ).toHaveTextContent("pip install 'spark-pulse[benchmarking]'");
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: /Start|Run/ })).toBeDisabled();
  });

  /** On the lab the click read as nothing: the benchmark failed at once and
   *  only the server log said why. */
  /** A stored failure from before is not news: neither the row nor the
   *  dialog brings it up. The dialog says what is in the way now. */
  it("brings up no past failure, on the row or in the dialog", async () => {
    vi.mocked(fetchBenchmarks).mockResolvedValue([
      benchmark({ deployment_id: "r1", status: "error", results: { error: "connection refused on :9000" } }),
    ]);
    show();

    const row = await screen.findByTestId("deployment-r1");
    await userEvent.setup().click(await within(row).findByRole("button", { name: "Benchmark" }));
    await screen.findByRole("dialog");
    expect(document.body).not.toHaveTextContent("connection refused");
    expect(row).not.toHaveTextContent("failed");
  });

  it("says the benchmark is measuring while it runs", async () => {
    vi.mocked(fetchBenchmarks).mockResolvedValue([
      benchmark({ deployment_id: "r1", status: "running", results: null, completed_at: null }),
    ]);
    show();

    const row = await screen.findByTestId("deployment-r1");
    expect(await within(row).findByText("measuring…")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: /Benchmark/ })).toBeDisabled();
  });

  it("says there are no runs at all rather than showing an empty pill", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([]);
    show();

    expect(await screen.findByText("No runs yet — deploy a recipe.")).toBeInTheDocument();
    expect(screen.getByText("Recipes are on the Deploy page.")).toBeInTheDocument();
  });

  it("says the live pill is empty without claiming the history is", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([FINISHED]);
    show();
    await openTab("Live");

    expect(await screen.findByText("Nothing is serving.")).toBeInTheDocument();
  });

  it("says the history is empty when everything is still running", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([RUNNING]);
    show();
    await openTab("Finished");

    expect(await screen.findByText("No finished runs.")).toBeInTheDocument();
  });

  it("surfaces a failed load instead of an empty list", async () => {
    vi.mocked(fetchDeployments).mockRejectedValue(new Error("API 503: backend restarting"));
    show();

    expect(await screen.findByText("API 503: backend restarting")).toBeInTheDocument();
    expect(screen.queryByText("No runs yet — deploy a recipe.")).toBeNull();
  });
});

describe("RunsPage row", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchDeployments).mockResolvedValue([SOLO, GANG, LEGACY]);
  });

  it("names the machines a run's ranks landed on, and the shape it was given", async () => {
    show();

    const row = await screen.findByTestId("deployment-gang");
    expect(row).toHaveTextContent("tp 2 · 10.0.0.10 + 10.0.0.11");
  });

  it("says nothing multi-node about a run on one machine", async () => {
    show();

    const row = await screen.findByTestId("deployment-solo");
    expect(row).toHaveTextContent("this node");
  });

  /** "this node :8000" from the lab: which node, and a port is not an address. */
  it("names the node a solo run is on, and gives an endpoint to copy", async () => {
    const onDesktop = deployment({
      id: "desk",
      name: "desk job",
      port: 8000,
      ranks: [{ rank: 0, node: "", host: "ark-desktop", container_name: "c", is_head: true }],
    });
    vi.mocked(fetchDeployments).mockResolvedValue([onDesktop]);
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    show();

    const row = await screen.findByTestId("deployment-desk");
    expect(row).toHaveTextContent("ark-desktop");
    expect(row).not.toHaveTextContent("this node");
    // The control node: the machine this page was reached on.
    const url = `http://${window.location.hostname}:8000/v1`;
    expect(within(row).getByTestId("run-endpoint")).toHaveTextContent(url);

    // The whole chip is the button. fireEvent, not userEvent: user-event
    // installs a clipboard of its own.
    fireEvent.click(within(row).getByRole("button", { name: `Copy the endpoint ${url}` }));
    expect(writeText).toHaveBeenCalledWith(url);
    // The answer is on the chip, where the operator is looking.
    await waitFor(() => expect(within(row).getByTestId("run-endpoint")).toHaveTextContent("Copied to clipboard"));
    // Announced too: the endpoint's live region says it (the curl has its own).
    expect(within(row).getAllByRole("status").map((e) => e.textContent)).toContain("Copied to clipboard");
  });

  it("copies a curl that asks the run for a fun fact, next to the endpoint", async () => {
    const onDesktop = deployment({
      id: "desk3",
      name: "desk job 3",
      port: 8000,
      served_model: "Qwen/Qwen2.5-0.5B-Instruct",
      ranks: [{ rank: 0, node: "", host: "ark-desktop", container_name: "c", is_head: true }],
    });
    vi.mocked(fetchDeployments).mockResolvedValue([onDesktop]);
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    show();

    const row = await screen.findByTestId("deployment-desk3");
    fireEvent.click(within(row).getByRole("button", { name: "Copy a curl that asks this run for a fun fact" }));

    const command = (writeText.mock.calls[0] as unknown as [string])[0];
    expect(command).toMatch(
      new RegExp(`^curl -s http://${window.location.hostname}:8000/v1/chat/completions `),
    );
    const body = JSON.parse(command.slice(command.indexOf("-d '") + 4, -1));
    expect(body.model).toBe("Qwen/Qwen2.5-0.5B-Instruct");
    expect(body.messages[0].content).toMatch(/^Tell me one surprising fun fact about .+, in two sentences\.$/);
    await waitFor(() => expect(within(row).getByTestId("run-curl")).toHaveTextContent("curl copied"));
  });

  /** "Saturn's rings" closed the shell's quote and broke the command. */
  it("builds a command a shell reads back whole, for every topic", () => {
    // What a POSIX shell makes of one single-quoted word written by shellQuote.
    const unquote = (word: string) => {
      expect(word.startsWith("'") && word.endsWith("'")).toBe(true);
      return word.slice(1, -1).split("'\\''").join("'");
    };
    expect(shellQuote("it's")).toBe("'it'\\''s'");
    expect(unquote(shellQuote("it's 'quoted'"))).toBe("it's 'quoted'");

    // Every topic, in both languages: French is full of apostrophes.
    for (const language of ["en", "fr"] as const) {
      const { t } = translatorFor(language);
      FUN_FACT_TOPICS.forEach((id, index) => {
        const command = curlExample("http://localhost:8000/v1", "m", t, () => index);
        const word = command.slice(command.indexOf(" -d ") + 4);
        const body = JSON.parse(unquote(word));
        expect(body.messages[0].content).toContain(t(`runs.funFact.topics.${id}`));
      });
    }
  });

  it("asks in the page's language", () => {
    const fr = curlExample("http://h/v1", "m", translatorFor("fr").t, () => 0);
    expect(fr).toContain("Donne-moi une anecdote surprenante sur les pieuvres");
    const en = curlExample("http://h/v1", "m", translatorFor("en").t, () => 0);
    expect(en).toContain("Tell me one surprising fun fact about octopuses");
  });

  it("says so on the chip when the clipboard refuses", async () => {
    const onDesktop = deployment({
      id: "desk2",
      name: "desk job 2",
      port: 8000,
      ranks: [{ rank: 0, node: "", host: "ark-desktop", container_name: "c", is_head: true }],
    });
    vi.mocked(fetchDeployments).mockResolvedValue([onDesktop]);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn(() => Promise.reject(new Error("denied"))) },
      configurable: true,
    });
    show();

    const row = await screen.findByTestId("deployment-desk2");
    fireEvent.click(within(row).getByRole("button", { name: /Copy the endpoint/ }));
    await waitFor(() => expect(within(row).getByTestId("run-endpoint")).toHaveTextContent("Could not copy"));
  });

  it("points a peer's run at that peer's address", async () => {
    const onPeer = deployment({
      id: "peer",
      name: "peer job",
      port: 9001,
      ranks: [{ rank: 0, node: "10.0.0.11", host: "spark-b", container_name: "c", is_head: true }],
    });
    vi.mocked(fetchDeployments).mockResolvedValue([onPeer]);
    show();

    const row = await screen.findByTestId("deployment-peer");
    expect(within(row).getByTestId("run-endpoint")).toHaveTextContent("http://10.0.0.11:9001/v1");
  });

  it("leaves a record with no node_count alone rather than failing on it", async () => {
    show();

    const row = await screen.findByTestId("deployment-legacy");
    expect(row).toHaveTextContent("legacy job");
  });

  it("shows the port, the engine and the model as chips", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "c1", name: "chipped", engine: "vllm", variant: "default", model: "Qwen/Qwen3-8B" }),
    ]);
    show();

    const row = await screen.findByTestId("deployment-c1");
    expect(row).toHaveTextContent("vllm/default");
    expect(row).toHaveTextContent("Qwen/Qwen3-8B");
    expect(row).toHaveTextContent(":9000");
  });

  /** The two numbers an operator arrives with a question about. They come from
   *  the run's own latest benchmark, and say "—" rather than nothing when
   *  nobody has measured it. */
  it("carries the latest benchmark's numbers on the row", async () => {
    vi.mocked(fetchBenchmarks).mockResolvedValue([
      benchmark({ benchmark_id: "old", started_at: "2026-01-01T00:00:00+00:00", results: { throughput: 1 } }),
      benchmark({ benchmark_id: "new" }),
    ]);
    show();

    const row = await screen.findByTestId("deployment-solo");
    await waitFor(() => expect(row).toHaveTextContent("45.2 tok/s"));
    expect(row).toHaveTextContent("12.3 ms");
  });

  it("writes an em dash for a run nobody has benchmarked", async () => {
    show();

    const row = await screen.findByTestId("deployment-legacy");
    expect(within(row).getByText("Throughput").parentElement).toHaveTextContent("—");
  });

  it("says how long a live run has been serving", async () => {
    show();

    const row = await screen.findByTestId("deployment-solo");
    expect(within(row).getByText("Serving")).toBeInTheDocument();
    expect(row).toHaveTextContent(/up \d/);
  });

  it("says when a finished run ended, and how long it lasted", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({
        id: "done",
        name: "done job",
        status: "stopped",
        started_at: "2026-01-01T10:00:00Z",
        stopped_at: "2026-01-01T12:30:00Z",
      }),
    ]);
    show();

    const row = await screen.findByTestId("deployment-done");
    expect(within(row).getByText("Ended")).toBeInTheDocument();
    expect(row).toHaveTextContent("2h 30m");
  });
});

/** The teardown path.
 *
 * One button does three different things depending on what the run is doing —
 * stop a running one, cancel a pending one, forget a finished one — and the
 * confirmation is the only place the operator is told which. Getting that
 * wrong means someone clicks "Stop" expecting a graceful shutdown and instead
 * erases the record of a run, or clicks expecting to tidy history and kills a
 * live model. So the wording of each of the three is pinned here.
 */
describe("RunsPage teardown", () => {
  const RUNNING = deployment({ id: "run1", name: "running job", status: "running" });
  const PENDING = deployment({ id: "pend1", name: "pending job", status: "pending" });
  const FINISHED = deployment({ id: "old1", name: "finished job", status: "stopped" });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(connectLogStream).mockReturnValue(() => {});
    vi.mocked(fetchDeployments).mockResolvedValue([RUNNING, PENDING, FINISHED]);
    vi.mocked(stopDeployment).mockResolvedValue(undefined);
  });

  it("calls stopping a running model what it is, and terminates it once confirmed", async () => {
    const user = userEvent.setup();
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Stop" }));

    expect(await screen.findByRole("heading", { name: "Stop this run" })).toBeInTheDocument();
    expect(screen.getByText(/Stop “running job”\? Its containers go/)).toBeInTheDocument();
    expect(stopDeployment).not.toHaveBeenCalled();

    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(stopDeployment).toHaveBeenCalledWith("run1"));
  });

  it("calls stopping a run that has not started yet a cancel", async () => {
    const user = userEvent.setup();
    show();

    const row = await screen.findByTestId("deployment-pend1");
    await user.click(within(row).getByRole("button", { name: "Cancel" }));

    expect(await screen.findByRole("heading", { name: "Cancel this run" })).toBeInTheDocument();
    expect(screen.getByText(/Cancel “pending job” before it starts\?/)).toBeInTheDocument();
  });

  it("calls clearing a finished run a removal, not a stop", async () => {
    const user = userEvent.setup();
    show();
    await openTab("Finished");

    const row = await screen.findByTestId("deployment-old1");
    await user.click(within(row).getByRole("button", { name: "Remove" }));

    expect(await screen.findByRole("heading", { name: "Remove this run" })).toBeInTheDocument();
    expect(screen.getByText(/Remove “finished job” from the history\?/)).toBeInTheDocument();
    expect(screen.queryByText(/Its containers go/)).toBeNull();

    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(stopDeployment).toHaveBeenCalledWith("old1"));
  });

  it("leaves the run alone when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Stop" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(stopDeployment).not.toHaveBeenCalled();
  });

  it("says why a stop failed instead of leaving the row looking untouched", async () => {
    const user = userEvent.setup();
    vi.mocked(stopDeployment).mockRejectedValue(new Error("API 500: container is wedged"));
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Stop" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Stop" }));

    expect(await screen.findByText("API 500: container is wedged")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("API 500: container is wedged")).toBeNull());
  });
});

/** The one launcher: it opens from the run it will measure, so there is no id
 *  to type and no way to point it at something that does not exist. */
describe("RunsPage benchmark launcher", () => {
  const RUNNING = deployment({ id: "run1", name: "running job", status: "running" });
  const PENDING = deployment({ id: "pend1", name: "pending job", status: "pending" });
  const FINISHED = deployment({ id: "old1", name: "finished job", status: "stopped" });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(connectLogStream).mockReturnValue(() => {});
    vi.mocked(fetchDeployments).mockResolvedValue([RUNNING, PENDING, FINISHED]);
  });

  it("offers a benchmark only for a model that is actually serving", async () => {
    show();

    const running = await screen.findByTestId("deployment-run1");
    expect(within(running).getByRole("button", { name: "Benchmark" })).toBeEnabled();
    expect(
      within(screen.getByTestId("deployment-pend1")).getByRole("button", { name: "Benchmark" }),
    ).toBeDisabled();
    await openTab("Finished");
    expect(
      within(screen.getByTestId("deployment-old1")).queryByRole("button", { name: "Benchmark" }),
    ).toBeNull();
  });

  it("names the run it is about to measure and sends its own id, not a typed one", async () => {
    const user = userEvent.setup();
    vi.mocked(runBenchmark).mockResolvedValue({ benchmark_id: "b1" } as never);
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Benchmark" }));

    expect(await screen.findByRole("heading", { name: "Benchmark this run" })).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-labelledby");
    expect(screen.getByText(/Measure “running job” while it serves/)).toBeInTheDocument();
    // No free-text target: there is nothing to mistype.
    expect(screen.queryByLabelText(/Target Deployment/)).toBeNull();

    await user.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() =>
      expect(runBenchmark).toHaveBeenCalledWith(
        expect.objectContaining({
          deployment_id: "run1",
          recipe_id: "qwen3-8b",
          recipe_name: "running job",
          params: { benchmarks: ["throughput", "latency"], context_length: 4096 },
        }),
      ),
    );
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Benchmark this run" })).toBeNull(),
    );
  });

  it("carries the metrics and the context length the operator chose", async () => {
    const user = userEvent.setup();
    vi.mocked(runBenchmark).mockResolvedValue({ benchmark_id: "b1" } as never);
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Benchmark" }));

    const dialog = within(screen.getByRole("dialog"));
    await user.click(dialog.getByLabelText("latency"));
    await user.click(dialog.getByLabelText("gpu memory"));
    fireEvent.change(dialog.getByLabelText("Context length"), { target: { value: "8192" } });
    await user.click(dialog.getByRole("button", { name: "Run" }));

    await waitFor(() =>
      expect(runBenchmark).toHaveBeenCalledWith(
        expect.objectContaining({
          params: { benchmarks: ["throughput", "gpu_memory"], context_length: 8192 },
        }),
      ),
    );
  });

  it("will not start a measurement of nothing", async () => {
    const user = userEvent.setup();
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Benchmark" }));
    const dialog = within(screen.getByRole("dialog"));
    await user.click(dialog.getByLabelText("throughput"));
    await user.click(dialog.getByLabelText("latency"));

    expect(dialog.getByRole("button", { name: "Run" })).toBeDisabled();
  });

  it("explains a refused benchmark rather than closing on a failure", async () => {
    const user = userEvent.setup();
    vi.mocked(runBenchmark).mockRejectedValue(new Error("benchmarking is disabled"));
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Benchmark" }));
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByText("benchmarking is disabled")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Benchmark this run" })).toBeInTheDocument();
  });

  it("backs out of a benchmark without running one", async () => {
    const user = userEvent.setup();
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Benchmark" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Benchmark this run" })).toBeNull(),
    );
    expect(runBenchmark).not.toHaveBeenCalled();
  });

  it("closes the launcher from its own X", async () => {
    const user = userEvent.setup();
    show();

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByRole("button", { name: "Benchmark" }));
    await user.click(within(screen.getByRole("dialog")).getByTitle("Close"));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Benchmark this run" })).toBeNull(),
    );
  });
});

/** What the expanded row shows: the container that is actually running, the
 *  ranks it spans, and the live log. */
/** Since a delete records an intent and returns, the row survives the request
 *  that asked for it to go. What the operator sees while the reconciler works
 *  is the whole point of the change: the record still says what it is, plus
 *  what has been asked of it, and the buttons that would ask again are shut. */
describe("RunsPage convergence", () => {
  const SETTLED = deployment({ id: "ok1", name: "settled job", status: "running", sync: "in_sync" });
  /** A live run the operator has asked to stop. It is still running: the
   *  containers are gone only when a node says so. */
  const GOING = deployment({
    id: "go1",
    name: "going job",
    status: "running",
    sync: "in_progress",
    sync_reason: "waiting on a node",
  });
  /** A finished run whose record is being cleared out of history. */
  const CLEARING = deployment({ id: "hist1", name: "cleared job", status: "stopped", sync: "deleting" });

  class CapturingEventSource {
    static instances: CapturingEventSource[] = [];
    url: string;
    readyState = 1;
    onmessage: ((e: MessageEvent) => void) | null = null;
    onopen: (() => void) | null = null;
    onerror: (() => void) | null = null;

    constructor(url: string) {
      this.url = url;
      CapturingEventSource.instances.push(this);
    }
    addEventListener() {}
    removeEventListener() {}
    close() {
      this.readyState = 2;
    }
    emit(payload: unknown) {
      this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
    }
  }

  beforeEach(() => {
    vi.clearAllMocks();
    CapturingEventSource.instances = [];
    vi.stubGlobal("EventSource", CapturingEventSource);
    vi.mocked(connectLogStream).mockReturnValue(() => {});
    vi.mocked(fetchDeployments).mockResolvedValue([SETTLED, GOING, CLEARING]);
    vi.mocked(stopDeployment).mockResolvedValue(undefined);
  });

  it("says a running run is still running while it is being stopped", async () => {
    show();

    const row = within(await screen.findByTestId("deployment-go1"));
    // Not "stopped": the container may still be holding its GPU.
    expect(row.getByText("Running")).toBeInTheDocument();
    expect(row.getByTestId("sync-in_progress")).toHaveAttribute("title", "waiting on a node");
  });

  it("shuts the stop button while the stop is in flight", async () => {
    show();

    const row = within(await screen.findByTestId("deployment-go1"));

    expect(row.getByTitle("Waiting for the nodes to catch up")).toBeDisabled();
  });

  it("shuts the remove button too, so the record is not asked twice", async () => {
    show();
    await openTab("Finished");

    const row = within(await screen.findByTestId("deployment-hist1"));

    expect(row.getByRole("button", { name: "Remove" })).toBeDisabled();
    expect(row.getByTitle("Waiting for the nodes to catch up")).toBeDisabled();
  });

  /** #205: the server's one phase is what the row shows, and what shuts the
   *  actions — not the pair, which a page used to combine itself. */
  it("shows a run's phase as its one state and shuts the stop while it settles", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({
        id: "ph1",
        name: "phased job",
        status: "running",
        sync: "in_progress",
        sync_intent: "stop",
        phase: "stopping",
        phase_verified: true,
        phase_reason: "stop requested",
      }),
    ]);
    show();

    const row = within(await screen.findByTestId("deployment-ph1"));
    expect(row.getByTestId("phase-stopping")).toHaveTextContent("Stopping…");
    expect(row.queryByText("Running")).toBeNull();
    expect(row.getByTitle("Waiting for the nodes to catch up")).toBeDisabled();
  });

  it("leaves a settled run's actions alone", async () => {
    show();

    const row = within(await screen.findByTestId("deployment-ok1"));

    expect(row.getByRole("button", { name: "Stop" })).toBeEnabled();
    expect(row.queryByTestId("sync-in_sync")).toBeNull();
  });

  /** Convergence changed the record, so the row on screen is stale. Waiting
   *  for the ten-second poll is what made a delete look like nothing had
   *  happened. */
  it("re-reads the list when the reconciler says a record changed", async () => {
    show();
    await screen.findByTestId("deployment-go1");
    await waitFor(() =>
      expect(CapturingEventSource.instances.find((s) => s.url === "/sse/events/deployments")).toBeDefined(),
    );
    const stream = CapturingEventSource.instances.find((s) => s.url === "/sse/events/deployments")!;
    const before = vi.mocked(fetchDeployments).mock.calls.length;

    act(() =>
      stream.emit({
        type: "deployment_sync",
        event_id: "e1",
        timestamp: "2026-01-01T00:00:00Z",
        message: "removal requested",
        resource: "go1",
        resource_type: "deployment",
      }),
    );

    await waitFor(() =>
      expect(vi.mocked(fetchDeployments).mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("re-reads the list on a lifecycle frame the stream carries", async () => {
    show();
    await screen.findByTestId("deployment-go1");
    await waitFor(() =>
      expect(CapturingEventSource.instances.find((s) => s.url === "/sse/events/deployments")).toBeDefined(),
    );
    const stream = CapturingEventSource.instances.find((s) => s.url === "/sse/events/deployments")!;
    const before = vi.mocked(fetchDeployments).mock.calls.length;

    act(() =>
      stream.emit({
        type: "deployment_stopped",
        event_id: "e3",
        timestamp: "2026-01-01T00:00:00Z",
        message: "rank 0 is gone",
        resource: "go1",
        resource_type: "deployment",
      }),
    );

    await waitFor(() =>
      expect(vi.mocked(fetchDeployments).mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("does not re-read the list for an ordinary log event", async () => {
    show();
    await screen.findByTestId("deployment-go1");
    await waitFor(() =>
      expect(CapturingEventSource.instances.find((s) => s.url === "/sse/events/deployments")).toBeDefined(),
    );
    const stream = CapturingEventSource.instances.find((s) => s.url === "/sse/events/deployments")!;
    const before = vi.mocked(fetchDeployments).mock.calls.length;

    act(() =>
      stream.emit({
        type: "deployment.started",
        event_id: "e2",
        timestamp: "2026-01-01T00:00:00Z",
        message: "rank 0 is serving",
        resource: "go1",
        resource_type: "deployment",
      }),
    );

    expect(vi.mocked(fetchDeployments).mock.calls.length).toBe(before);
  });
});

/** The fallback poll.
 *
 * It exists for what the stream cannot say — a dropped connection, a frame the
 * backend does not send. Nothing live means nothing can change without a
 * frame, so an idle control plane is asked for nothing at all.
 */
describe("RunsPage polling", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("re-reads the list while something is live", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([SOLO]);
    show();
    await vi.waitFor(() => expect(screen.getByTestId("deployment-solo")).toBeInTheDocument());
    const before = vi.mocked(fetchDeployments).mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });

    expect(vi.mocked(fetchDeployments).mock.calls.length).toBeGreaterThan(before);
  });

  it("asks for nothing when every run has finished", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "f1", name: "finished job", status: "stopped" }),
    ]);
    show();
    await vi.waitFor(() => expect(screen.getByTestId("deployment-f1")).toBeInTheDocument());
    const before = vi.mocked(fetchDeployments).mock.calls.length;

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });

    expect(vi.mocked(fetchDeployments).mock.calls.length).toBe(before);
  });
});

/** On a wide screen Runs is a console table, full width. A run opens on its
 *  own page rather than in a panel beside the list. */
describe("RunsPage — the wide layout", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1400 });
    vi.mocked(fetchDeployments).mockResolvedValue([
      SOLO,
      GANG,
      deployment({ id: "pending", name: "pending job", status: "pending", phase: "queued", port: null }),
      deployment({
        id: "done",
        name: "done job",
        status: "stopped",
        phase: "stopped",
        started_at: "2026-01-01T00:00:00+00:00",
        stopped_at: "2026-01-01T01:30:00+00:00",
      }),
    ]);
    vi.mocked(fetchBenchmarks).mockResolvedValue([benchmark()]);
  });

  afterEach(() => {
    Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1024 });
  });

  it("lists the runs as table rows with the figures in columns", async () => {
    show();
    const table = await screen.findByTestId("runs-table");
    const row = within(table).getByTestId("deployment-solo");
    expect(row).toHaveTextContent(":9000");
    expect(await within(row).findByText("45.2 tok/s")).toBeInTheDocument();
    expect(within(table).getByTestId("deployment-pending")).toHaveTextContent("—");
    expect(within(table).queryByTestId("deployment-done")).toBeNull();
    expect(within(table).getByRole("columnheader", { name: "Model" })).toBeInTheDocument();
    // A row, not a card: the row carries the run name as a table cell.
    expect(screen.getByTestId("deployment-solo").tagName).toBe("TR");
  });

  it("opens a run's page from its name, and from anywhere on its row", async () => {
    const user = userEvent.setup();
    show();
    const table = await screen.findByTestId("runs-table");
    expect(within(table).getByRole("link", { name: "gang job" })).toHaveAttribute("href", "/jobs/gang");

    await user.click(within(table).getByTestId("deployment-gang"));
    expect(await screen.findByTestId("run-detail-gang")).toBeInTheDocument();
    expect(screen.queryByTestId("runs-table")).toBeNull();
  });

  it("tears a run down from its row without opening it", async () => {
    const user = userEvent.setup();
    show();
    const table = await screen.findByTestId("runs-table");
    await user.click(within(table).getByRole("button", { name: "Cancel pending job" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("pending job");
    expect(screen.queryByTestId("run-detail-pending")).toBeNull();
  });

  it("says when a finished run ended and how long it ran", async () => {
    const user = userEvent.setup();
    show();
    await openTab("Finished");
    const row = await screen.findByTestId("deployment-done");
    expect(row).toHaveTextContent(/ran 1h 30m/);
    await user.click(within(row).getByRole("button", { name: "Remove done job" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("done job");
  });
});
