/** The Inference list: a deployment that spans machines says so on its row
 * and repeats what is unproven when it is opened; a solo one says nothing. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import InferencePage from "@/pages/InferencePage";
import { MULTI_NODE_BADGE_TITLE, MULTI_NODE_UNPROVEN } from "@/lib/experimental";
import type { Deployment, EngineMetricsWindow } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  // Inert by default: only the tests that care about rank state stub it.
  fetchDeployment: vi.fn(() => Promise.resolve(undefined)),
  fetchDeployments: vi.fn(),
  // Also inert by default: the metrics panel is exercised in its own file and
  // in the tests below that stub this deliberately.
  fetchEngineMetrics: vi.fn(() => Promise.resolve(undefined)),
  stopDeployment: vi.fn(),
  connectLogStream: vi.fn(() => () => {}),
  runBenchmark: vi.fn(),
}));

import {
  connectLogStream,
  fetchDeployment,
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

const SOLO = deployment();
const GANG = deployment({
  id: "gang",
  name: "gang job",
  node_count: 2,
  nodes: ["10.0.0.10", "10.0.0.11"],
});
/** A record written before the field existed. */
const LEGACY = deployment({ id: "legacy", name: "legacy job", node_count: undefined });

async function expand(name: string) {
  const user = userEvent.setup();
  await user.click(await screen.findByText(name));
}

describe("InferencePage multi-node marking", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchDeployments).mockResolvedValue([SOLO, GANG, LEGACY]);
  });

  it("says how many machines a multi-node deployment holds, and that it is unverified", async () => {
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-gang");
    expect(row).toHaveTextContent("2 nodes");
    expect(within(row).getByTitle(MULTI_NODE_BADGE_TITLE)).toBeInTheDocument();
  });

  it("says nothing multi-node about a deployment on one machine", async () => {
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-solo");
    expect(row).not.toHaveTextContent(/nodes/);
    expect(within(row).queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
  });

  it("leaves a record with no node_count alone rather than failing on it", async () => {
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-legacy");
    expect(row).toHaveTextContent("legacy job");
    expect(within(row).queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
  });

  it("carries the unproven list into the expanded view of a multi-node deployment", async () => {
    render(<InferencePage />);
    await expand("gang job");

    const note = within(screen.getByTestId("deployment-gang")).getByRole("note");
    expect(note).toHaveTextContent(/implemented but unverified/i);
    for (const item of MULTI_NODE_UNPROVEN) {
      expect(note).toHaveTextContent(item);
    }
  });

  it("does not warn about multi-node when a solo deployment is opened", async () => {
    render(<InferencePage />);
    await expand("solo job");

    const panel = screen.getByTestId("deployment-solo");
    expect(within(panel).queryByRole("note")).toBeNull();
    // Opened, not merely unchanged: the log pane is there.
    expect(within(panel).getByText("No logs yet...")).toBeInTheDocument();
  });

  it("opens an older record with no node_count without warning about machines it has none of", async () => {
    render(<InferencePage />);
    await expand("legacy job");

    const panel = screen.getByTestId("deployment-legacy");
    expect(within(panel).queryByRole("note")).toBeNull();
    expect(within(panel).getByText("No logs yet...")).toBeInTheDocument();
  });
});

/** The benchmark modal is a hand-rolled div rather than a `role="dialog"`,
 *  so its buttons are reached through its heading. The row's own icon buttons
 *  are titled "Stop" and "Cancel" too, which is why every confirmation click
 *  below is scoped to the modal rather than to the page. */
function benchmarkDialog(): HTMLElement {
  return screen
    .getByRole("heading", { name: "Run Benchmark" })
    .closest("div.rounded-xl") as HTMLElement;
}

/** The teardown path.
 *
 * One button does three different things depending on what the deployment is
 * doing — stop a running one, cancel a pending one, forget a finished one —
 * and the confirmation is the only place the operator is told which. Getting
 * that wrong means someone clicks "Stop" expecting a graceful shutdown and
 * instead erases the record of a run, or clicks expecting to tidy history and
 * kills a live model. So the wording of each of the three is pinned here.
 */
describe("InferencePage teardown", () => {
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
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByTitle("Stop"));

    expect(await screen.findByRole("heading", { name: "Stop Deployment" })).toBeInTheDocument();
    expect(
      screen.getByText(/Stop "running job"\? This will terminate the running process\./),
    ).toBeInTheDocument();
    expect(stopDeployment).not.toHaveBeenCalled();

    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(stopDeployment).toHaveBeenCalledWith("run1"));
  });

  it("calls stopping a deployment that has not started yet a cancel", async () => {
    const user = userEvent.setup();
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-pend1");
    await user.click(within(row).getByTitle("Cancel"));

    expect(await screen.findByRole("heading", { name: "Cancel" })).toBeInTheDocument();
    expect(screen.getByText(/Cancel "pending job" before it starts\?/)).toBeInTheDocument();
  });

  it("calls clearing a finished run a removal from history, not a stop", async () => {
    const user = userEvent.setup();
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-old1");
    await user.click(within(row).getByTitle("Remove from history"));

    expect(await screen.findByRole("heading", { name: "Remove" })).toBeInTheDocument();
    expect(screen.getByText(/Remove "finished job" from history\?/)).toBeInTheDocument();
    expect(screen.queryByText(/terminate the running process/)).toBeNull();

    await user.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(stopDeployment).toHaveBeenCalledWith("old1"));
  });

  it("leaves the deployment alone when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByTitle("Stop"));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(stopDeployment).not.toHaveBeenCalled();
  });

  it("says why a stop failed instead of leaving the row looking untouched", async () => {
    const user = userEvent.setup();
    vi.mocked(stopDeployment).mockRejectedValue(new Error("API 500: container is wedged"));
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByTitle("Stop"));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Stop" }));

    expect(await screen.findByText("API 500: container is wedged")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("API 500: container is wedged")).toBeNull());
  });

  it("offers a benchmark only for a model that is actually serving", async () => {
    render(<InferencePage />);

    const running = await screen.findByTestId("deployment-run1");
    expect(within(running).getByTitle("Run Benchmark")).toBeInTheDocument();
    expect(within(screen.getByTestId("deployment-pend1")).queryByTitle("Run Benchmark")).toBeNull();
    expect(within(screen.getByTestId("deployment-old1")).queryByTitle("Run Benchmark")).toBeNull();
  });

  it("names the deployment it is about to benchmark, and runs it on confirmation", async () => {
    const user = userEvent.setup();
    vi.mocked(runBenchmark).mockResolvedValue({ id: "bench1" } as never);
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByTitle("Run Benchmark"));

    expect(await screen.findByRole("heading", { name: "Run Benchmark" })).toBeInTheDocument();
    expect(screen.getByText("running job", { selector: "strong" })).toBeInTheDocument();
    expect(runBenchmark).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() =>
      expect(runBenchmark).toHaveBeenCalledWith(
        expect.objectContaining({ deployment_id: "run1", recipe_id: "qwen3-8b" }),
      ),
    );
    // The dialog closes itself once the run has been accepted.
    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Run Benchmark" })).toBeNull(),
    );
  });

  it("explains a refused benchmark rather than closing on a failure", async () => {
    const user = userEvent.setup();
    vi.mocked(runBenchmark).mockRejectedValue(new Error("benchmarking is disabled"));
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByTitle("Run Benchmark"));
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByText("benchmarking is disabled")).toBeInTheDocument();
  });

  it("backs out of a benchmark without running one", async () => {
    const user = userEvent.setup();
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-run1");
    await user.click(within(row).getByTitle("Run Benchmark"));
    await user.click(within(benchmarkDialog()).getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Run Benchmark" })).toBeNull(),
    );
    expect(runBenchmark).not.toHaveBeenCalled();
  });
});

/** What the expanded row shows: the container that is actually running, the
 *  ranks it spans, and the live log. */
describe("InferencePage expanded detail", () => {
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

  it("names the image and container a native deployment is actually running", async () => {
    render(<InferencePage />);
    await expand("native job");

    const panel = screen.getByTestId("deployment-nat1");
    expect(within(panel).getByText("vllm/default")).toBeInTheDocument();
    expect(within(panel).getByText("ghcr.io/acme/engine/vllm:0.1.0")).toBeInTheDocument();
    expect(
      within(panel).getByText("spark-pulse-nat1-r0-g1", { selector: "dd" }),
    ).toBeInTheDocument();
  });

  it("lists every rank with the machine it landed on, rank 0 marked head", async () => {
    render(<InferencePage />);
    await expand("native job");

    const ranks = within(screen.getByTestId("deployment-nat1")).getByTestId("rank-rows");
    expect(within(ranks).getByTestId("rank-row-0")).toHaveTextContent("192.168.1.100");
    expect(within(ranks).getByTestId("rank-row-0")).toHaveTextContent("head");
    expect(within(ranks).getByTestId("rank-row-1")).toHaveTextContent("10.0.0.11");
    expect(within(ranks).getByTestId("rank-row-1")).not.toHaveTextContent("head");
  });

  it("streams the log and stops the stream when the row is closed again", async () => {
    const user = userEvent.setup();
    const close = vi.fn();
    let push: ((event: string, data: unknown) => void) | undefined;
    vi.mocked(connectLogStream).mockImplementation((_id, onMessage) => {
      push = onMessage;
      return close;
    });
    render(<InferencePage />);
    await expand("native job");

    expect(connectLogStream).toHaveBeenCalledWith("nat1", expect.any(Function));
    expect(screen.getByText("Streaming")).toBeInTheDocument();

    act(() => push!("log", { text: "INFO Application startup complete." }));
    expect(await screen.findByText("INFO Application startup complete.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(close).toHaveBeenCalled();
    expect(screen.queryByText("Streaming")).toBeNull();
  });

  /** A `status` frame means the deployment moved on — the row's badge is
   *  stale until the list is re-read, so the stream has to trigger that. */
  it("re-reads the deployment list when the stream reports a status change", async () => {
    let push: ((event: string, data: unknown) => void) | undefined;
    vi.mocked(connectLogStream).mockImplementation((_id, onMessage) => {
      push = onMessage;
      return () => {};
    });
    render(<InferencePage />);
    await expand("native job");
    const before = vi.mocked(fetchDeployments).mock.calls.length;

    act(() => push!("status", { status: "stopped" }));

    await waitFor(() =>
      expect(vi.mocked(fetchDeployments).mock.calls.length).toBeGreaterThan(before),
    );
  });

  it("says there is nothing to show rather than rendering an empty list", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([]);
    render(<InferencePage />);

    expect(await screen.findByText("No deployments yet.")).toBeInTheDocument();
    expect(screen.getByText("Launch a recipe from the Recipes page.")).toBeInTheDocument();
  });

  it("surfaces a failed load instead of an empty list", async () => {
    vi.mocked(fetchDeployments).mockRejectedValue(new Error("API 503: backend restarting"));
    render(<InferencePage />);

    expect(await screen.findByText("API 503: backend restarting")).toBeInTheDocument();
    expect(screen.queryByText("No deployments yet.")).toBeNull();
  });

  /** The log pane auto-scrolls, but only while the operator is already at the
   *  bottom — scrolling up to read something must not be yanked away by the
   *  next line. All that can be observed here is that scrolling is handled at
   *  all rather than throwing. */
  it("tracks whether the log pane is pinned to the bottom", async () => {
    render(<InferencePage />);
    await expand("native job");

    const pane = screen.getByText("No logs yet...").parentElement!;
    fireEvent.scroll(pane, { target: { scrollTop: 0 } });

    expect(screen.getByText("No logs yet...")).toBeInTheDocument();
  });

  it("closes the benchmark dialog from its own X", async () => {
    const user = userEvent.setup();
    render(<InferencePage />);

    const row = await screen.findByTestId("deployment-nat1");
    await user.click(within(row).getByTitle("Run Benchmark"));
    const dialog = screen
      .getByRole("heading", { name: "Run Benchmark" })
      .closest("div.rounded-xl") as HTMLElement;

    // The X carries no label of its own; it is the first button in the header.
    await user.click(within(dialog).getAllByRole("button")[0]);

    await waitFor(() =>
      expect(screen.queryByRole("heading", { name: "Run Benchmark" })).toBeNull(),
    );
  });
});

/** The deployment event stream.
 *
 * `/sse/deployments` is how the page learns that something happened to a
 * deployment it is not tailing the log of. The events are filtered per
 * deployment, so a frame for one job must not appear under another — that
 * filter is the whole reason the viewer is inside the expanded row.
 */
describe("InferencePage event stream", () => {
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

  const stream = () =>
    CapturingEventSource.instances.find((s) => s.url === "/sse/deployments")!;

  it("shows an event under the deployment it belongs to, and not under another", async () => {
    render(<InferencePage />);
    await expand("first job");
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

    const first = screen.getByTestId("deployment-d1");
    expect(await within(first).findByText("rank 0 is serving")).toBeInTheDocument();

    // The other deployment's viewer is not even mounted, and once it is, the
    // event does not belong to it.
    await expand("first job"); // close
    await expand("second job");
    const second = screen.getByTestId("deployment-d2");
    expect(within(second).queryByText("rank 0 is serving")).toBeNull();
    expect(within(second).getByText("No events to display")).toBeInTheDocument();
  });

  it("fills in an id and a timestamp for a frame that arrived without them", async () => {
    render(<InferencePage />);
    await expand("first job");
    await waitFor(() => expect(stream()).toBeDefined());

    act(() => stream().emit({ type: "deployment.error", resource: "d1" }));

    // It is listed rather than dropped, which is the point: an event with a
    // missing field is still evidence something happened.
    const first = screen.getByTestId("deployment-d1");
    await waitFor(() =>
      expect(within(first).queryByText("No events to display")).toBeNull(),
    );
  });

  it("ignores a frame that is not an event at all", async () => {
    render(<InferencePage />);
    await expand("first job");
    await waitFor(() => expect(stream()).toBeDefined());

    act(() => stream().emit({ heartbeat: true }));

    expect(
      within(screen.getByTestId("deployment-d1")).getByText("No events to display"),
    ).toBeInTheDocument();
  });

  it("clears the events of one deployment without touching the others", async () => {
    const user = userEvent.setup();
    render(<InferencePage />);
    await expand("first job");
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
    const first = screen.getByTestId("deployment-d1");
    await within(first).findByText("rank 0 is serving");

    await user.click(within(first).getByRole("button", { name: /clear/i }));

    expect(within(first).getByText("No events to display")).toBeInTheDocument();
  });
});

/** Which machine in a cluster is sick.
 *
 * Per-rank container state is not in the deployment list and deliberately so:
 * the list is one Docker enumerate on this machine, while a rank's live state
 * is an inspect per rank — over SSH for every rank that is not local. It is
 * read from the detail endpoint for the one row the operator has opened, which
 * is the only place the ranks are rendered anyway. These tests pin both halves:
 * the highlight reaches the screen, and nothing is spent on rows nobody opened.
 */
describe("InferencePage rank health", () => {
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
    render(<InferencePage />);
    await expand("cluster job");

    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledWith("c1"));
    const rows = within(screen.getByTestId("deployment-c1")).getByTestId("rank-rows");
    const sick = await within(rows).findByTestId("rank-row-1");
    expect(sick).toHaveTextContent("exited");
    expect(sick).toHaveTextContent("10.0.0.11");
    expect(sick.className).toContain("border-danger");

    const well = within(rows).getByTestId("rank-row-0");
    expect(well).toHaveTextContent("running");
    expect(well.className).not.toContain("border-danger");
  });

  it("spends nothing on the ranks of rows nobody has opened", async () => {
    render(<InferencePage />);

    await screen.findByTestId("deployment-c1");
    expect(fetchDeployment).not.toHaveBeenCalled();
  });

  it("does not inspect the ranks of a deployment that is not running", async () => {
    // A stopped deployment has no containers by design and a pending one has
    // none yet: asking would paint every rank red for saying what the row says.
    vi.mocked(fetchDeployments).mockResolvedValue([
      deployment({ id: "c1", name: "cluster job", status: "stopped", ranks: RANKS }),
    ]);
    render(<InferencePage />);
    await expand("cluster job");

    expect(
      within(screen.getByTestId("deployment-c1")).getByTestId("rank-rows"),
    ).toBeInTheDocument();
    expect(fetchDeployment).not.toHaveBeenCalled();
  });

  it("keeps the ranks the list already gave when the live read fails", async () => {
    vi.mocked(fetchDeployment).mockRejectedValue(new Error("API 502: node unreachable"));
    render(<InferencePage />);
    await expand("cluster job");

    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledWith("c1"));
    const rows = within(screen.getByTestId("deployment-c1")).getByTestId("rank-rows");
    expect(within(rows).getByTestId("rank-row-1")).toHaveTextContent("10.0.0.11");
    // No container state, and no error banner shouted over the log pane.
    expect(within(rows).getByTestId("rank-row-1")).not.toHaveTextContent("exited");
    expect(screen.queryByText("API 502: node unreachable")).toBeNull();
  });

  it("re-reads the rank state when the stream reports a status change", async () => {
    let push: ((event: string, data: unknown) => void) | undefined;
    vi.mocked(connectLogStream).mockImplementation((_id, onMessage) => {
      push = onMessage;
      return () => {};
    });
    render(<InferencePage />);
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
    render(<InferencePage />);
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
    render(<InferencePage />);
    await expand("cluster job");
    await within(screen.getByTestId("deployment-c1")).findByText("exited");

    await expand("other job");

    await waitFor(() => expect(fetchDeployment).toHaveBeenCalledWith("c2"));
    const rows = within(screen.getByTestId("deployment-c2")).getByTestId("rank-rows");
    expect(rows).not.toHaveTextContent("exited");
  });
});

// ── Engine metrics on the open row ───────────────────────────────────────────

describe("InferencePage engine metrics", () => {
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
    render(<InferencePage />);
    await screen.findByText("metrics job");

    expect(fetchEngineMetrics).not.toHaveBeenCalled();
  });

  it("reads the open row's window and shows the queue depth", async () => {
    vi.mocked(fetchEngineMetrics).mockResolvedValue(metricsWindow());
    render(<InferencePage />);

    await expand("metrics job");

    await waitFor(() => expect(fetchEngineMetrics).toHaveBeenCalledWith("m1"));
    const row = within(screen.getByTestId("deployment-m1"));
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
    render(<InferencePage />);

    await expand("metrics job");

    expect(
      await screen.findByText("SGLang serves /metrics only with --enable-metrics."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Queued")).toBeNull();
  });

  it("stops saying it is reading when the read fails, and invents nothing", async () => {
    vi.mocked(fetchEngineMetrics).mockRejectedValue(new Error("network"));
    render(<InferencePage />);

    await expand("metrics job");

    expect(await screen.findByText("No engine metrics yet.")).toBeInTheDocument();
    expect(screen.queryByText("Queued")).toBeNull();
  });

  it("does not show one row's window against another", async () => {
    const OTHER = deployment({ id: "m2", name: "other metrics job", status: "running" });
    vi.mocked(fetchDeployments).mockResolvedValue([RUNNING, OTHER]);
    // Only ever answers for m1; opening m2 must therefore show nothing.
    vi.mocked(fetchEngineMetrics).mockResolvedValue(metricsWindow());
    render(<InferencePage />);
    await expand("metrics job");
    await screen.findByText("Queued");

    await expand("other metrics job");

    const other = within(screen.getByTestId("deployment-m2"));
    expect(other.queryByText("Queued")).toBeNull();
  });
});
