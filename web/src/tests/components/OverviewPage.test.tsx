/** Overview: the fleet on one screen.
 *
 * What is worth pinning: a node card per node with its memory cut by run (and
 * a node that cannot be asked keeping its card, saying why), the live runs
 * linking to their row on Runs, a fleet problem said once, and the activity
 * column seeded from stored timelines so a page opened late is not empty.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import OverviewPage from "@/pages/OverviewPage";
import NodeCard, { nodeCondition } from "@/components/overview/NodeCard";
import type { ClusterNode, Deployment, MemoryResponse, NodeStats } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchDeployments: vi.fn(),
  fetchDeploymentEvents: vi.fn(),
  fetchMemory: vi.fn(),
  connectMetricsStream: vi.fn(),
  fetchNodes: vi.fn(),
  fetchNodeDiagnostics: vi.fn(),
  fetchRecipes: vi.fn(() => Promise.resolve([])),
}));

import {
  connectMetricsStream,
  fetchDeploymentEvents,
  fetchDeployments,
  fetchMemory,
  fetchNodeDiagnostics,
  fetchNodes,
} from "@/lib/api";

const GB = 1024;

function stats(over: Partial<NodeStats> = {}): NodeStats {
  return {
    id: "n1",
    name: "spark-01",
    address: "10.0.0.1",
    is_control_plane: true,
    reachable: true,
    error: null,
    unavailable: [],
    gpu: [
      {
        index: 0,
        gpu: "0",
        uuid: "g",
        name: "GB10",
        memory_total: 0,
        memory_used: 0,
        memory_free: 0,
        memory_supported: false,
        temperature: 58,
        utilization: 71,
        power_draw: 112.4,
        power_limit: 140,
      },
    ],
    cpu: { total: 121 * GB, used: 95 * GB, free: 26 * GB, available: 26 * GB, usage_percent: 78 },
    disk: [],
    processes: [{ gpu_uuid: "g", pid: 9, process_name: "vllm", used_memory: 83 * GB, deployment: "r1" }],
    ...over,
  };
}

function run(over: Partial<Deployment> = {}): Deployment {
  return {
    id: "r1",
    recipe_id: "bundled/qwen",
    name: "qwen3.8-27b",
    params: {},
    nodes: ["10.0.0.1"],
    status: "running",
    phase: "running",
    pid: null,
    port: 9000,
    created_at: "2026-11-22T10:00:00Z",
    started_at: "2026-11-22T10:00:00Z",
    stopped_at: null,
    error_message: null,
    engine: "vllm",
    ...over,
  };
}

const NODE = { id: "n1", name: "spark-01", state: "healthy", agent: { enrolled: true, connected: true, current: true } } as unknown as ClusterNode;

function Where() {
  const location = useLocation();
  return <p data-testid="where">{location.pathname + location.search}</p>;
}

function show() {
  return render(
    <MemoryRouter>
      <OverviewPage />
      <Where />
    </MemoryRouter>,
  );
}

let pushMetrics: ((event: string, data: unknown) => void) | null = null;

beforeEach(() => {
  vi.clearAllMocks();
  pushMetrics = null;
  vi.mocked(fetchMemory).mockResolvedValue({ nodes: [stats(), stats({ id: "n2", name: "spark-02", is_control_plane: false, processes: [] })] });
  vi.mocked(connectMetricsStream).mockImplementation((cb) => {
    pushMetrics = cb;
    return () => {};
  });
  vi.mocked(fetchDeployments).mockResolvedValue([run(), run({ id: "old", name: "old run", status: "stopped", phase: "stopped", created_at: "2026-11-01T00:00:00Z" })]);
  vi.mocked(fetchNodes).mockResolvedValue([NODE]);
  vi.mocked(fetchNodeDiagnostics).mockResolvedValue({
    findings: [
      { code: "dup", severity: "warning", summary: "2 nodes report the same machine-id", remedy: "", node_ids: ["n1", "n2"] },
      { code: "mdns", severity: "info", summary: "mDNS is off", remedy: "", node_ids: [] },
    ],
  });
  vi.mocked(fetchDeploymentEvents).mockImplementation((id) =>
    Promise.resolve({
      resource: id,
      total: 1,
      limit: 8,
      events:
        id === "r1"
          ? [
              { event_id: "e1", type: "deployment_ready", message: "ready in 94s", resource: "r1", timestamp: "2026-11-22T10:02:00Z" },
              { event_id: "e2", type: "deployment_log", message: "a log line", resource: "r1", timestamp: "2026-11-22T10:03:00Z" },
              { event_id: "e3", type: "deployment_error", message: "boom", resource: "r1", severity: "error", timestamp: "2026-11-22T10:01:00Z" },
            ]
          : [],
    }),
  );
});

describe("OverviewPage", () => {
  it("draws a card per node with its memory cut by run", async () => {
    show();
    const card = await screen.findByTestId("node-card-n1");
    expect(within(card).getByText("spark-01")).toBeInTheDocument();
    expect(within(card).getByText("Control plane")).toBeInTheDocument();
    expect(within(card).getByRole("img")).toHaveAttribute("aria-label", "Unified memory: 26.0 GB free of 121.0 GB");
    expect(within(card).getByText("qwen3.8-27b")).toBeInTheDocument();
    expect(within(card).getByText("71%")).toBeInTheDocument();
    expect(within(card).getByText("58°")).toBeInTheDocument();
    expect(within(card).getByText("112 W")).toBeInTheDocument();
    expect(screen.getByText("2 nodes · 1 run serving")).toBeInTheDocument();
  });

  it("follows the metrics stream once it speaks", async () => {
    show();
    await screen.findByTestId("node-card-n1");
    const next: MemoryResponse = { nodes: [stats({ name: "renamed" })] };
    act(() => {
      pushMetrics?.("metrics", next);
      pushMetrics?.("other", {});
    });
    expect(screen.getByText("renamed")).toBeInTheDocument();
    expect(screen.queryByTestId("node-card-n2")).toBeNull();
  });

  it("lists the live runs, linking each to its own page", async () => {
    show();
    const table = await screen.findByTestId("overview-runs");
    expect(within(table).getByText("qwen3.8-27b")).toBeInTheDocument();
    expect(within(table).queryByText("old run")).toBeNull();
    expect(within(table).getByText(":9000")).toBeInTheDocument();
    fireEvent.click(within(table).getByText("qwen3.8-27b"));
    expect(screen.getByTestId("where")).toHaveTextContent("/jobs/r1");
  });

  it("puts the runs that just ended under the live ones", async () => {
    show();
    const recent = await screen.findByTestId("overview-recent");
    expect(within(recent).getByText("old run")).toBeInTheDocument();
    expect(within(recent).queryByText("qwen3.8-27b")).toBeNull();
  });

  it("says a fleet warning once, and leaves the informational ones to Fleet", async () => {
    show();
    const findings = await screen.findByTestId("fleet-findings");
    expect(findings).toHaveTextContent("2 nodes report the same machine-id");
    expect(findings).not.toHaveTextContent("mDNS");
  });

  it("seeds the activity from stored timelines, without log lines", async () => {
    show();
    const activity = await screen.findByTestId("overview-activity");
    expect(await within(activity).findByText("ready in 94s")).toBeInTheDocument();
    expect(within(activity).getByText("boom").closest("[title]")).toHaveClass("text-bad");
    expect(within(activity).queryByText("a log line")).toBeNull();
  });

  it("offers a way to deploy when nothing is running", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([]);
    vi.mocked(fetchNodeDiagnostics).mockResolvedValue({ findings: [] });
    show();
    expect(await screen.findByText("Nothing is running.")).toBeInTheDocument();
    expect(screen.getByText("Nothing has happened yet.")).toBeInTheDocument();
    expect(screen.queryByTestId("fleet-findings")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Browse recipes" }));
    expect(screen.getByTestId("where")).toHaveTextContent("/recipes");
  });

  it("says why when the runs or the stats cannot be read", async () => {
    vi.mocked(fetchDeployments).mockRejectedValue(new Error("runs unavailable"));
    vi.mocked(fetchMemory).mockRejectedValue(new Error("stats unavailable"));
    vi.mocked(fetchDeploymentEvents).mockRejectedValue(new Error("no"));
    show();
    expect(await screen.findByText("runs unavailable")).toBeInTheDocument();
    expect(await screen.findByText("stats unavailable")).toBeInTheDocument();
  });

  it("shows no uptime for a run that is not yet serving", async () => {
    vi.mocked(fetchDeployments).mockResolvedValue([
      run({ status: "pulling", phase: "pulling", port: null, started_at: null }),
    ]);
    show();
    const table = await screen.findByTestId("overview-runs");
    expect(within(table).getAllByText("—").length).toBeGreaterThanOrEqual(2);
  });
});

describe("NodeCard", () => {
  it("keeps the card for a node that did not answer, and says why", () => {
    render(
      <MemoryRouter>
        <NodeCard stats={stats({ reachable: false, error: "agent unreachable", gpu: [] })} layout={null} />
      </MemoryRouter>,
    );
    expect(screen.getAllByText("agent unreachable").length).toBeGreaterThan(0);
    expect(screen.getAllByText("—")).toHaveLength(3);
  });

  it("falls back to a generic line when an answering node reports no memory", () => {
    render(
      <MemoryRouter>
        <NodeCard stats={stats({ is_control_plane: false })} layout={null} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Memory not reported")).toBeInTheDocument();
  });

  it("reads the condition from reachability, the agent and the registry", () => {
    expect(nodeCondition(stats({ reachable: false }))).toBe("bad");
    expect(nodeCondition(stats())).toBe("ok");
    expect(nodeCondition(stats(), { ...NODE, state: "dead" })).toBe("bad");
    expect(nodeCondition(stats(), { ...NODE, state: "weird" as never })).toBe("unknown");
    expect(
      nodeCondition(stats(), { ...NODE, agent: { enrolled: true, connected: true, current: false } }),
    ).toBe("warn");
  });

  it("names an outdated agent on the card", () => {
    render(
      <MemoryRouter>
        <NodeCard
          stats={stats()}
          node={{ ...NODE, agent: { enrolled: true, connected: true, current: false } }}
          layout={{
            totalMb: 100,
            usedMb: 30,
            freeMb: 70,
            segments: [
              { kind: "system", id: "", label: "", mb: 10, color: "x" },
              { kind: "other", id: "", label: "", mb: 20, color: "y" },
            ],
          }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("Agent outdated")).toBeInTheDocument();
    expect(screen.getByTitle(/System and other processes/)).toBeInTheDocument();
    expect(screen.getByTitle(/not started by Spark Pulse/)).toBeInTheDocument();
  });
});
