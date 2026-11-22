/** A run's own page: its overview, its logs, its events.
 *
 * Everything an opened run carries — the log stream, the per-rank read, the
 * engine's own metrics window and the deployment event stream — reached the
 * way an operator reaches it: from the list, by the run's name.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import RunsPage from "@/pages/RunsPage";
import RunDetailPage from "@/pages/RunDetailPage";
import type { BenchmarkResult, Deployment, EngineMetricsWindow } from "@/lib/types";

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
  connectLogStream,
  fetchBenchmarks,
  fetchDeployment,
  fetchDeploymentEvents,
  fetchDeployments,
  fetchEngineMetrics,
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

/** Open a run's page from the list, and one of its tabs. From another run's
 *  page, the back link comes first — which is what an operator does too. */
async function expand(name: string, tab?: "Logs" | "Events") {
  const user = userEvent.setup();
  const back = screen.queryByRole("link", { name: "Runs" });
  if (back) await user.click(back);
  await user.click(await screen.findByRole("link", { name }));
  if (tab) await user.click(await screen.findByRole("tab", { name: new RegExp(`^${tab}`) }));
}

beforeEach(() => {
  appConfig.benchmarking_enabled = true;
  appConfig.benchmarking_tool = undefined;
});

describe("RunDetailPage overview and logs", () => {
  const NATIVE = deployment({
    id: "nat1",
    name: "native job",
    status: "running",
    runtime: "native",
    engine: "vllm",
    variant: "default",
    image_ref: "ghcr.io/acme/engine/vllm:0.1.0",
    model: "Qwen/Qwen3-8B",
    container_name: "spark-pulse-nat1-r0-g1",
    node_count: 2,
    ranks: [
      {
        rank: 0,
        node: "192.168.1.100",
        host: "192.168.1.100",
        container_name: "spark-pulse-nat1-r0-g1",
        is_head: true,
      },
      {
        rank: 1,
        node: "10.0.0.11",
        host: "10.0.0.11",
        container_name: "spark-pulse-nat1-r1-g1",
        is_head: false,
      },
    ],
    orphans: [],
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchDeployments).mockResolvedValue([NATIVE]);
    vi.mocked(connectLogStream).mockReturnValue(() => {});
  });

  /** A multi-node run's expanded view carried a warning box naming what had
   *  not been run on hardware. Two nodes have run and been measured, so the
   *  expanded view is the run's facts and nothing else. */
  it("warns about nothing when a multi-node run is opened", async () => {
    show();
    await expand("native job");

    expect(within(screen.getByTestId("run-detail-nat1")).queryByRole("note")).toBeNull();
  });

  it("does not warn about multi-node when a solo run is opened", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([SOLO]);
    show();
    await expand("solo job", "Logs");

    const panel = screen.getByTestId("run-detail-solo");
    expect(within(panel).queryByRole("note")).toBeNull();
    expect(within(panel).getByText("No logs yet…")).toBeInTheDocument();
  });

  it("opens an older record with no node_count without warning about machines it has none of", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([LEGACY]);
    show();
    await expand("legacy job", "Logs");

    const panel = screen.getByTestId("run-detail-legacy");
    expect(within(panel).queryByRole("note")).toBeNull();
    expect(within(panel).getByText("No logs yet…")).toBeInTheDocument();
  });

  it("names the recipe, image and container a run is actually using", async () => {
    show();
    await expand("native job");

    const panel = screen.getByTestId("run-detail-nat1");
    expect(within(panel).getByText("qwen3-8b", { selector: "dd" })).toBeInTheDocument();
    expect(within(panel).getByText("vllm/default", { selector: "dd" })).toBeInTheDocument();
    expect(within(panel).getByText("ghcr.io/acme/engine/vllm:0.1.0")).toBeInTheDocument();
    expect(
      within(panel).getByText("spark-pulse-nat1-r0-g1", { selector: "dd" }),
    ).toBeInTheDocument();
  });

  it("lists every rank with the machine it landed on, rank 0 marked head", async () => {
    show();
    await expand("native job");

    const ranks = within(screen.getByTestId("run-detail-nat1")).getByTestId("rank-rows");
    expect(within(ranks).getByTestId("rank-row-0")).toHaveTextContent("192.168.1.100");
    expect(within(ranks).getByTestId("rank-row-0")).toHaveTextContent("head");
    expect(within(ranks).getByTestId("rank-row-1")).toHaveTextContent("10.0.0.11");
    expect(within(ranks).getByTestId("rank-row-1")).not.toHaveTextContent("head");
  });

  it("streams the log and stops the stream when the page is left", async () => {
    const user = userEvent.setup();
    const close = vi.fn();
    let push: ((event: string, data: unknown) => void) | undefined;
    vi.mocked(connectLogStream).mockImplementation((_id, onMessage) => {
      push = onMessage;
      return close;
    });
    show();
    await expand("native job", "Logs");

    expect(connectLogStream).toHaveBeenCalledWith("nat1", expect.any(Function));
    expect(screen.getByText("Streaming")).toBeInTheDocument();

    act(() => push!("log", { text: "INFO Application startup complete." }));
    expect(await screen.findByText("INFO Application startup complete.")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Runs" }));
    expect(close).toHaveBeenCalled();
    expect(screen.queryByText("Streaming")).toBeNull();
  });

  /** A `status` frame means the run moved on — the row's badge is stale until
   *  the list is re-read, so the stream has to trigger that. */
  it("re-reads the list when the stream reports a status change", async () => {
    let push: ((event: string, data: unknown) => void) | undefined;
    vi.mocked(connectLogStream).mockImplementation((_id, onMessage) => {
      push = onMessage;
      return () => {};
    });
    show();
    await expand("native job");
    const before = vi.mocked(fetchDeployments).mock.calls.length;

    act(() => push!("status", { status: "stopped" }));

    await waitFor(() =>
      expect(vi.mocked(fetchDeployments).mock.calls.length).toBeGreaterThan(before),
    );
  });

  /** The server sends `end` once there will be nothing more, and the client
   *  closes the connection on it — otherwise `EventSource` reconnects by
   *  itself and replays the whole log every few seconds. The badge has to
   *  follow, or the row claims to be streaming a stream that is shut. */
  it("stops claiming to stream once the server has ended it", async () => {
    let push: ((event: string, data: unknown) => void) | undefined;
    vi.mocked(connectLogStream).mockImplementation((_id, onMessage) => {
      push = onMessage;
      return () => {};
    });
    show();
    await expand("native job", "Logs");
    expect(screen.getByText("Streaming")).toBeInTheDocument();

    act(() => push!("end", {}));

    expect(screen.queryByText("Streaming")).toBeNull();
  });

  /** The log pane auto-scrolls, but only while the operator is already at the
   *  bottom — scrolling up to read something must not be yanked away by the
   *  next line. All that can be observed here is that scrolling is handled at
   *  all rather than throwing. */
  it("tracks whether the log pane is pinned to the bottom", async () => {
    show();
    await expand("native job", "Logs");

    const pane = screen.getByText("No logs yet…").parentElement!;
    fireEvent.scroll(pane, { target: { scrollTop: 0 } });

    expect(screen.getByText("No logs yet…")).toBeInTheDocument();
  });
});

/** The deployment event stream.
 *
 * `/sse/events/deployments` is how the page learns that something happened to a
 * run it is not tailing the log of. The events are filtered per run, so a
 * frame for one must not appear under another — that filter is the whole
 * reason the viewer is inside the expanded row.
 */
describe("RunDetailPage events", () => {
  const ONE = deployment({ id: "d1", name: "first job", status: "running" });
  const TWO = deployment({ id: "d2", name: "second job", status: "running" });

  /** The shared setupTests EventSource stub records listeners but cannot
   *  deliver frames; this one can. */
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
    vi.mocked(fetchDeployments).mockResolvedValue([ONE, TWO]);
    vi.mocked(connectLogStream).mockReturnValue(() => {});
  });

  /** The newest connection: the run's page opens its own when it mounts, and
   *  the list's closed with the list. */
  const stream = () =>
    CapturingEventSource.instances.filter((s) => s.url === "/sse/events/deployments").at(-1)!;

  /** The lab: a multi-GB engine image read "Pulling" for ten minutes, with
   *  nothing about how far it had got. */
  it("shows a pulling run's progress beside its status and under its chips", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "p1", name: "pulling job", status: "pulling" }),
    ]);
    show();
    await screen.findByTestId("deployment-p1");
    await waitFor(() => expect(stream()).toBeDefined());

    act(() =>
      stream().emit({
        type: "image.pull.progress",
        event_id: "pp1",
        timestamp: "2026-01-01T00:00:00Z",
        message: "pulling img: 45%",
        resource: "p1",
        resource_type: "deployment",
        metadata: { percent: 45.67, bytes_done: 3_800_000_000, bytes_total: 8_400_000_000 },
      }),
    );

    const row = screen.getByTestId("deployment-p1");
    expect(await within(row).findByText("Pulling… 45%")).toBeInTheDocument();
    expect(within(row).getByTestId("pull-p1")).toHaveTextContent(/Pulling the engine image: 45% \(.+ of .+\)/);
    expect(within(row).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "45");
  });

  it("shows an event under the run it belongs to, and not under another", async () => {
    show();
    await expand("first job", "Events");
    await waitFor(() => expect(stream()).toBeDefined());

    act(() =>
      stream().emit({
        type: "deployment.started",
        event_id: "e1",
        timestamp: "2026-01-01T00:00:00Z",
        message: "rank 0 is serving",
        resource: "d1",
        resource_type: "deployment",
      }),
    );

    const first = screen.getByTestId("run-detail-d1");
    expect(await within(first).findByText("rank 0 is serving")).toBeInTheDocument();

    // The other run's viewer is not even mounted, and once it is, the event
    // does not belong to it.
    await expand("second job", "Events");
    const second = screen.getByTestId("run-detail-d2");
    expect(within(second).queryByText("rank 0 is serving")).toBeNull();
    expect(within(second).getByText("No events to display")).toBeInTheDocument();
  });

  it("fills in an id and a timestamp for a frame that arrived without them", async () => {
    show();
    await expand("first job", "Events");
    await waitFor(() => expect(stream()).toBeDefined());

    act(() => stream().emit({ type: "deployment.error", resource: "d1" }));

    // It is listed rather than dropped, which is the point: an event with a
    // missing field is still evidence something happened.
    const first = screen.getByTestId("run-detail-d1");
    await waitFor(() =>
      expect(within(first).queryByText("No events to display")).toBeNull(),
    );
  });

  it("ignores a frame that is not an event at all", async () => {
    show();
    await expand("first job", "Events");
    await waitFor(() => expect(stream()).toBeDefined());

    act(() => stream().emit({ heartbeat: true }));

    expect(
      within(screen.getByTestId("run-detail-d1")).getByText("No events to display"),
    ).toBeInTheDocument();
  });

  it("seeds the panel from the run's stored history", async () => {
    // The defect this fixes: everything below happened before the page was
    // opened, so the stream carries none of it and the panel said "No events
    // to display" under a footer promising thirty days of retention.
    vi.mocked(fetchDeploymentEvents).mockResolvedValue({
      resource: "d1",
      events: [
        {
          event_id: "h2",
          timestamp: "2026-01-01T00:01:00Z",
          type: "deployment_ready",
          message: "qwen3 is serving on port 8000",
          resource: "d1",
          resource_type: "deployment",
          node: "",
          severity: "info",
        },
        {
          event_id: "h1",
          timestamp: "2026-01-01T00:00:00Z",
          type: "deployment_planned",
          message: "planned qwen3 on vllm",
          resource: "d1",
          resource_type: "deployment",
          node: "",
          severity: "info",
        },
      ],
      total: 2,
      limit: 200,
    });

    show();
    await expand("first job", "Events");

    const first = screen.getByTestId("run-detail-d1");
    expect(await within(first).findByText("planned qwen3 on vllm")).toBeInTheDocument();
    expect(within(first).getByText("qwen3 is serving on port 8000")).toBeInTheDocument();
    expect(within(first).queryByText("No events to display")).toBeNull();
    expect(fetchDeploymentEvents).toHaveBeenCalledWith("d1", { limit: 200 });
  });

  it("shows the latest events beside the details, and links to all of them", async () => {
    const many = Array.from({ length: 14 }, (_, i) => ({
      event_id: `e${i}`,
      timestamp: `2026-01-01T00:${String(59 - i).padStart(2, "0")}:00Z`,
      type: "deployment_progress",
      message: `step ${i}`,
      resource: "d1",
      resource_type: "deployment",
      node: "",
      severity: i === 0 ? "error" : i === 1 ? "warning" : "info",
    }));
    vi.mocked(fetchDeploymentEvents).mockResolvedValue({ resource: "d1", events: many, total: 14, limit: 200 });

    show();
    await expand("first job");

    const recent = await screen.findByTestId("run-recent-events");
    expect(await within(recent).findByText("step 0")).toHaveClass("text-bad");
    expect(within(recent).getByText("step 1")).toHaveClass("text-warn");
    expect(within(recent).getByText("step 11")).toBeInTheDocument();
    expect(within(recent).queryByText("step 12")).toBeNull();

    await userEvent.setup().click(within(recent).getByRole("button", { name: "All events" }));
    expect(screen.getByRole("tab", { name: /^Events/ })).toHaveAttribute("aria-selected", "true");
  });

  it("says so when a run has no events yet", async () => {
    vi.mocked(fetchDeploymentEvents).mockResolvedValue({ resource: "d1", events: [], total: 0, limit: 200 });
    show();
    await expand("first job");
    const recent = await screen.findByTestId("run-recent-events");
    expect(await within(recent).findByText("No events yet.")).toBeInTheDocument();
    expect(within(recent).queryByRole("button", { name: "All events" })).toBeNull();
  });

  it("counts everything the store holds, not the page it was sent", async () => {
    vi.mocked(fetchDeploymentEvents).mockResolvedValue({
      resource: "d1",
      events: [
        {
          event_id: "h1",
          timestamp: "2026-01-01T00:00:00Z",
          type: "deployment_planned",
          message: "planned",
          resource: "d1",
          resource_type: "deployment",
          node: "",
          severity: "info",
        },
      ],
      total: 412,
      limit: 200,
    });

    show();
    await expand("first job", "Events");

    const first = screen.getByTestId("run-detail-d1");
    expect(await within(first).findByText("412 events")).toBeInTheDocument();
  });

  it("appends a live frame to the history without showing it twice", async () => {
    vi.mocked(fetchDeploymentEvents).mockResolvedValue({
      resource: "d1",
      events: [
        {
          event_id: "e1",
          timestamp: "2026-01-01T00:00:00Z",
          type: "deployment_planned",
          message: "planned qwen3 on vllm",
          resource: "d1",
          resource_type: "deployment",
          node: "",
          severity: "info",
        },
      ],
      total: 1,
      limit: 200,
    });

    show();
    await expand("first job", "Events");
    await waitFor(() => expect(stream()).toBeDefined());
    const first = screen.getByTestId("run-detail-d1");
    await within(first).findByText("planned qwen3 on vllm");

    // The same event again — the id is what makes that knowable, and it is
    // minted on the backend precisely so the two sources can agree.
    act(() =>
      stream().emit({
        event_id: "e1",
        type: "deployment_planned",
        timestamp: "2026-01-01T00:00:00Z",
        message: "planned qwen3 on vllm",
        resource: "d1",
        resource_type: "deployment",
      }),
    );
    // And one that really is new.
    act(() =>
      stream().emit({
        event_id: "e2",
        type: "deployment_ready",
        timestamp: "2026-01-01T00:02:00Z",
        message: "qwen3 is serving on port 8000",
        resource: "d1",
        resource_type: "deployment",
      }),
    );

    expect(
      await within(first).findByText("qwen3 is serving on port 8000"),
    ).toBeInTheDocument();
    expect(within(first).getAllByText("planned qwen3 on vllm")).toHaveLength(1);
    expect(within(first).getByText("2 events")).toBeInTheDocument();
  });

  it("keeps the live frames when the history cannot be read", async () => {
    vi.mocked(fetchDeploymentEvents).mockRejectedValue(new Error("nope"));

    show();
    await expand("first job", "Events");
    await waitFor(() => expect(stream()).toBeDefined());

    act(() =>
      stream().emit({
        event_id: "e9",
        type: "deployment_error",
        timestamp: "2026-01-01T00:03:00Z",
        message: "the engine exited",
        resource: "d1",
        resource_type: "deployment",
      }),
    );

    const first = screen.getByTestId("run-detail-d1");
    expect(await within(first).findByText("the engine exited")).toBeInTheDocument();
  });

  it("clears the events of one run without touching the others", async () => {
    const user = userEvent.setup();
    show();
    await expand("first job", "Events");
    await waitFor(() => expect(stream()).toBeDefined());

    act(() =>
      stream().emit({
        type: "deployment.started",
        event_id: "e1",
        message: "rank 0 is serving",
        resource: "d1",
        resource_type: "deployment",
      }),
    );
    const first = screen.getByTestId("run-detail-d1");
    await within(first).findByText("rank 0 is serving");

    await user.click(within(first).getByRole("button", { name: /clear/i }));

    expect(within(first).getByText("No events to display")).toBeInTheDocument();
  });
});

/** Which machine in a cluster is sick.
 *
 * Per-rank container state is not in the deployment list and deliberately so:
 * the list is one enumerate, while a rank's live state is an inspect per rank
 * through that rank's own node. It is read from the detail endpoint for the
 * one row the operator has opened, which is the only place the ranks are
 * rendered anyway. These tests pin both halves: the highlight reaches the
 * screen, and nothing is spent on rows nobody opened.
 */
describe("RunDetailPage rank health", () => {
  const RANKS = [
    {
      rank: 0,
      node: "192.168.1.100",
      host: "192.168.1.100",
      container_name: "spark-pulse-c1-r0-g1",
      is_head: true,
    },
    {
      rank: 1,
      node: "10.0.0.11",
      host: "10.0.0.11",
      container_name: "spark-pulse-c1-r1-g1",
      is_head: false,
    },
  ];
  const CLUSTER = deployment({
    id: "c1",
    name: "cluster job",
    status: "running",
    runtime: "native",
    node_count: 2,
    ranks: RANKS,
    orphans: [],
  });
  /** What the detail endpoint adds that the list does not: each rank's container. */
  const LIVE = {
    ...CLUSTER,
    ranks: [
      {
        ...RANKS[0],
        container: { status: "running", running: true, id: "a", state: {}, error: null },
      },
      {
        ...RANKS[1],
        container: { status: "exited", running: false, id: "b", state: {}, error: null },
      },
    ],
  } as Deployment;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(connectLogStream).mockReturnValue(() => {});
    vi.mocked(fetchDeployments).mockResolvedValue([CLUSTER]);
    vi.mocked(fetchDeployment).mockResolvedValue(LIVE);
  });

  it("names the rank whose container died, and leaves the healthy one plain", async () => {
    show();
    await expand("cluster job");

    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledWith("c1"));
    const rows = within(screen.getByTestId("run-detail-c1")).getByTestId("rank-rows");
    const sick = await within(rows).findByTestId("rank-row-1");
    // The one status vocabulary, not a third set of words for containers.
    expect(sick).toHaveTextContent("Exited");
    expect(sick).toHaveTextContent("10.0.0.11");
    expect(sick.className).toContain("border-bad");

    const well = within(rows).getByTestId("rank-row-0");
    expect(well).toHaveTextContent("Running");
    expect(well.className).not.toContain("border-bad");
  });

  it("spends nothing on the ranks of rows nobody has opened", async () => {
    show();

    await screen.findByTestId("deployment-c1");
    expect(fetchDeployment).not.toHaveBeenCalled();
  });

  it("does not inspect the ranks of a run that is not running", async () => {
    // A stopped run has no containers by design and a pending one has none
    // yet: asking would paint every rank red for saying what the row says.
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "c1", name: "cluster job", status: "stopped", ranks: RANKS }),
    ]);
    show();
    await expand("cluster job");

    expect(
      within(screen.getByTestId("run-detail-c1")).getByTestId("rank-rows"),
    ).toBeInTheDocument();
    expect(fetchDeployment).not.toHaveBeenCalled();
  });

  it("keeps the ranks the list already gave when the live read fails", async () => {
    vi.mocked(fetchDeployment).mockRejectedValue(new Error("API 502: node unreachable"));
    show();
    await expand("cluster job");

    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledWith("c1"));
    const rows = within(screen.getByTestId("run-detail-c1")).getByTestId("rank-rows");
    expect(within(rows).getByTestId("rank-row-1")).toHaveTextContent("10.0.0.11");
    // No container state, and no error banner shouted over the log pane.
    expect(within(rows).getByTestId("rank-row-1")).not.toHaveTextContent("Exited");
    expect(screen.queryByText("API 502: node unreachable")).toBeNull();
  });

  it("re-reads the rank state when the stream reports a status change", async () => {
    let push: ((event: string, data: unknown) => void) | undefined;
    vi.mocked(connectLogStream).mockImplementation((_id, onMessage) => {
      push = onMessage;
      return () => {};
    });
    show();
    await expand("cluster job");
    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledTimes(1));

    act(() => push!("status", { status: "stopped" }));

    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledTimes(2));
  });

  it("does not chase a status frame for a row whose ranks it never asked about", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "c1", name: "cluster job", status: "stopped", ranks: RANKS }),
    ]);
    let push: ((event: string, data: unknown) => void) | undefined;
    vi.mocked(connectLogStream).mockImplementation((_id, onMessage) => {
      push = onMessage;
      return () => {};
    });
    show();
    await expand("cluster job");

    act(() => push!("status", { status: "stopped" }));

    await waitFor(() =>
      expect(vi.mocked(fetchDeployments).mock.calls.length).toBeGreaterThan(1),
    );
    expect(fetchDeployment).not.toHaveBeenCalled();
  });

  it("forgets one row's rank state when another is opened", async () => {
    const OTHER = deployment({
      id: "c2",
      name: "other job",
      status: "running",
      ranks: RANKS,
    });
    vi.mocked(fetchDeployments).mockResolvedValue([CLUSTER, OTHER]);
    vi.mocked(fetchDeployment).mockImplementation(async (id: string) =>
      id === "c1" ? LIVE : ({ ...OTHER, ranks: RANKS } as Deployment),
    );
    show();
    await expand("cluster job");
    await within(screen.getByTestId("run-detail-c1")).findByText("Exited");

    await expand("other job");

    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledWith("c2"));
    const rows = within(screen.getByTestId("run-detail-c2")).getByTestId("rank-rows");
    expect(rows).not.toHaveTextContent("Exited");
  });
});

// ── Engine metrics on the open row ───────────────────────────────────────────

describe("RunDetailPage engine metrics", () => {
  const RUNNING = deployment({ id: "m1", name: "metrics job", status: "running" });

  function metricsWindow(over: Partial<EngineMetricsWindow> = {}): EngineMetricsWindow {
    return {
      deployment_id: "m1",
      available: true,
      reason: null,
      detail: null,
      sample_interval_seconds: 5,
      window_seconds: 3600,
      volatile: true,
      samples: [
        {
          t: 1_700_000_000,
          running: 1,
          waiting: 0,
          kv_fraction: 0.2,
          prompt_tokens_total: 10,
          generation_tokens_total: 5,
          preemptions_total: 0,
          prompt_tokens_per_second: null,
          generation_tokens_per_second: null,
          preemptions_per_second: null,
          counter_reset: false,
        },
        {
          t: 1_700_000_005,
          running: 3,
          waiting: 12,
          kv_fraction: 0.5,
          prompt_tokens_total: 110,
          generation_tokens_total: 55,
          preemptions_total: 1,
          prompt_tokens_per_second: 20,
          generation_tokens_per_second: 10,
          preemptions_per_second: 0.2,
          counter_reset: false,
        },
      ],
      ...over,
    };
  }

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(connectLogStream).mockReturnValue(() => {});
    vi.mocked(fetchDeployment).mockResolvedValue(undefined as unknown as Deployment);
    vi.mocked(fetchDeployments).mockResolvedValue([RUNNING]);
  });

  it("asks for nothing until a row is opened", async () => {
    show();
    await screen.findByText("metrics job");

    expect(fetchEngineMetrics).not.toHaveBeenCalled();
  });

  it("reads the open row's window and shows the queue depth", async () => {
    vi.mocked(fetchEngineMetrics).mockResolvedValue(metricsWindow());
    show();

    await expand("metrics job");

    await waitFor(() => expect(fetchEngineMetrics).toHaveBeenCalledWith("m1"));
    const row = within(screen.getByTestId("run-detail-m1"));
    expect(await row.findByText("Queued")).toBeInTheDocument();
    expect(row.getByText("Queued").parentElement).toHaveTextContent("12");
  });

  it("shows the reason instead of a chart when the engine publishes nothing", async () => {
    vi.mocked(fetchEngineMetrics).mockResolvedValue(
      metricsWindow({
        available: false,
        reason: "not_enabled",
        detail: "SGLang serves /metrics only with --enable-metrics.",
        samples: [],
      }),
    );
    show();

    await expand("metrics job");

    expect(
      await screen.findByText("SGLang serves /metrics only with --enable-metrics."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Queued")).toBeNull();
  });

  it("stops saying it is reading when the read fails, and invents nothing", async () => {
    vi.mocked(fetchEngineMetrics).mockRejectedValue(new Error("network"));
    show();

    await expand("metrics job");

    expect(await screen.findByText("No engine metrics yet.")).toBeInTheDocument();
    expect(screen.queryByText("Queued")).toBeNull();
  });

  it("does not show one row's window against another", async () => {
    const OTHER = deployment({ id: "m2", name: "other metrics job", status: "running" });
    vi.mocked(fetchDeployments).mockResolvedValue([RUNNING, OTHER]);
    // Only ever answers for m1; opening m2 must therefore show nothing.
    vi.mocked(fetchEngineMetrics).mockResolvedValue(metricsWindow());
    show();
    await expand("metrics job");
    await screen.findByText("Queued");

    await expand("other metrics job");

    const other = within(screen.getByTestId("run-detail-m2"));
    expect(other.queryByText("Queued")).toBeNull();
  });
});


/** The page itself: an address, a header that says what the run is and what
 *  can be done to it, and three tabs — none of them a scroll inside a scroll. */
describe("RunDetailPage header and tabs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(connectLogStream).mockReturnValue(() => {});
    vi.mocked(fetchDeployments).mockResolvedValue([
      SOLO,
      GANG,
      deployment({ id: "done", name: "done job", status: "stopped", phase: "stopped" }),
    ]);
    vi.mocked(fetchBenchmarks).mockResolvedValue([benchmark()]);
  });

  it("opens from the list on the overview, with the latest benchmark's figures", async () => {
    show();
    await expand("solo job");

    const page = await screen.findByTestId("run-detail-solo");
    expect(within(page).getByRole("heading", { level: 1, name: "solo job" })).toBeInTheDocument();
    expect(within(page).getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(await within(page).findByText("45.2 tok/s")).toBeInTheDocument();
    expect(within(page).getByText("12.3 ms")).toBeInTheDocument();
    expect(within(page).getByTestId("run-endpoint")).toHaveTextContent(":9000/v1");
  });

  it("names a multi-node run's shape and machines in the header", async () => {
    show("/jobs/gang");
    const page = await screen.findByTestId("run-detail-gang");
    expect(page).toHaveTextContent("tp 2 · 10.0.0.10 + 10.0.0.11");
  });

  it("opens the tab a link names", async () => {
    show("/jobs/solo?tab=logs");
    const page = await screen.findByTestId("run-detail-solo");
    expect(within(page).getByRole("tab", { name: "Logs" })).toHaveAttribute("aria-selected", "true");
    expect(within(page).getByText("No logs yet…")).toBeInTheDocument();
  });

  it("goes back to the list from its back link", async () => {
    const user = userEvent.setup();
    show("/jobs/solo?tab=events");
    await screen.findByTestId("run-detail-solo");
    await user.click(screen.getByRole("tab", { name: "Overview" }));
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");

    await user.click(screen.getByRole("link", { name: "Runs" }));
    expect(await screen.findByTestId("deployment-solo")).toBeInTheDocument();
  });

  it("says when there is no such run, and links back to the list", async () => {
    show("/jobs/gone");
    expect(await screen.findByText("No run with this id.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See the runs." })).toHaveAttribute("href", "/jobs");
  });

  it("says why the list could not be read rather than that the run is gone", async () => {
    vi.mocked(fetchDeployments).mockRejectedValue(new Error("API 500: boom"));
    show("/jobs/solo");
    expect(await screen.findByText(/boom/)).toBeInTheDocument();
    expect(screen.queryByText("No run with this id.")).toBeNull();
  });

  it("stops a live run once confirmed, and stays on its page", async () => {
    const user = userEvent.setup();
    vi.mocked(stopDeployment).mockResolvedValue(undefined as never);
    show("/jobs/solo");
    const page = await screen.findByTestId("run-detail-solo");

    await user.click(within(page).getByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("solo job");
    await user.click(within(dialog).getByRole("button", { name: "Stop" }));

    await waitFor(() => expect(stopDeployment).toHaveBeenCalledWith("solo"));
    expect(screen.getByTestId("run-detail-solo")).toBeInTheDocument();
  });

  it("goes back to the list once a finished run is removed", async () => {
    const user = userEvent.setup();
    vi.mocked(stopDeployment).mockResolvedValue(undefined as never);
    show("/jobs/done");
    const page = await screen.findByTestId("run-detail-done");

    await user.click(within(page).getByRole("button", { name: "Remove" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove" }));

    await waitFor(() => expect(stopDeployment).toHaveBeenCalledWith("done"));
    expect(await screen.findByRole("tab", { name: /^Finished/ })).toBeInTheDocument();
  });

  it("says why a stop failed", async () => {
    const user = userEvent.setup();
    vi.mocked(stopDeployment).mockRejectedValue(new Error("node unreachable"));
    show("/jobs/solo");
    const page = await screen.findByTestId("run-detail-solo");

    await user.click(within(page).getByRole("button", { name: "Stop" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Stop" }));

    expect(await screen.findByText("node unreachable")).toBeInTheDocument();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("node unreachable")).toBeNull());
  });

  it("starts a benchmark from the header and reads the history again", async () => {
    const user = userEvent.setup();
    vi.mocked(runBenchmark).mockResolvedValue({ benchmark_id: "b2" } as never);
    show("/jobs/solo");
    const page = await screen.findByTestId("run-detail-solo");
    await waitFor(() => expect(fetchBenchmarks).toHaveBeenCalled());
    const reads = vi.mocked(fetchBenchmarks).mock.calls.length;

    await user.click(within(page).getByRole("button", { name: /Benchmark/ }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Run" }));

    await waitFor(() => expect(runBenchmark).toHaveBeenCalledWith(expect.objectContaining({ deployment_id: "solo" })));
    await waitFor(() => expect(vi.mocked(fetchBenchmarks).mock.calls.length).toBeGreaterThan(reads));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("says the benchmark is measuring while it runs", async () => {
    vi.mocked(fetchBenchmarks).mockResolvedValue([benchmark({ status: "running", results: null })]);
    show("/jobs/solo");
    const page = await screen.findByTestId("run-detail-solo");
    expect(await within(page).findByText("measuring…")).toBeInTheDocument();
  });

  it("puts a failed run's reason on its overview", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "bad", name: "bad job", status: "error", phase: "error", error_message: "the engine exited at t=0.4s" }),
    ]);
    show("/jobs/bad");
    expect(await screen.findByText("the engine exited at t=0.4s")).toBeInTheDocument();
  });

  it("opens the benchmark launcher from the header, and closes it", async () => {
    const user = userEvent.setup();
    show("/jobs/solo");
    const page = await screen.findByTestId("run-detail-solo");

    await user.click(within(page).getByRole("button", { name: /Benchmark/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Benchmark this run" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("offers no benchmark for a run that has finished", async () => {
    show("/jobs/done");
    const page = await screen.findByTestId("run-detail-done");
    expect(within(page).queryByRole("button", { name: /Benchmark/ })).toBeNull();
  });
});
