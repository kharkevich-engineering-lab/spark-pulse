/** The pieces a node's page is made of, at their edges: nothing reported,
 *  readings the hardware declines to give, and a kill that fails. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NodeCharts, NodeProcesses, NodeStatStrip, NodeStorage } from "@/components/nodes/NodeLive";
import type { ClusterNode, NodeStats } from "@/lib/types";

const NODE = { id: "n", name: "n", address: "", is_control_plane: true, state: "healthy" } as ClusterNode;

vi.mock("@/lib/api", () => ({ killGpuProcess: vi.fn() }));
import { killGpuProcess } from "@/lib/api";

function stats(over: Partial<NodeStats> = {}): NodeStats {
  return {
    id: "n", name: "", address: "", is_control_plane: true, reachable: true, error: null, unavailable: [],
    gpu: [], cpu: { total: 0, used: 0, free: 0, available: 0, usage_percent: 0 }, disk: [], processes: [],
    ...over,
  } as NodeStats;
}

describe("NodeLive pieces", () => {
  beforeEach(() => vi.clearAllMocks());

  it("says what is not reported rather than drawing zeros", () => {
    render(
      <>
        <NodeStatStrip node={NODE} interfaces="eth0" stats={stats()} layout={null} loading={false} />
        <NodeStorage stats={stats()} />
        <NodeProcesses stats={stats()} onChanged={() => {}} />
      </>,
    );
    expect(screen.getByText("Memory not reported")).toBeInTheDocument();
    expect(screen.getByText("No GPU reported.")).toBeInTheDocument();
    expect(screen.queryByText("Power")).toBeNull();
    expect(screen.getByText("No agent")).toBeInTheDocument();
    expect(screen.getByText("This node reports no memory or disks.")).toBeInTheDocument();
    expect(screen.getByText("Nothing is holding the GPU.")).toBeInTheDocument();
  });

  it("waits, then says the node could not be asked, when the stats do not name it", () => {
    const { rerender } = render(<NodeStatStrip node={NODE} interfaces="" stats={undefined} layout={null} loading />);
    expect(screen.getByRole("status")).toBeInTheDocument();
    rerender(<NodeStatStrip node={NODE} interfaces="" stats={undefined} layout={null} loading={false} />);
    expect(screen.getByText("Could not be asked")).toBeInTheDocument();
  });

  it("renders a GPU that declines every figure, and no memory bar for it", () => {
    render(
      <NodeStatStrip
        node={NODE}
        interfaces=""
        layout={null}
        loading={false}
        stats={stats({
          gpu: [
            {
              index: 0, gpu: "0", uuid: "", name: "", memory_total: 0, memory_used: 0, memory_free: 0, memory_supported: false,
              temperature: 70, utilization: null, power_draw: null, power_limit: null,
            } as never,
          ],
        })}
      />,
    );
    expect(screen.getByText("70°C")).toHaveClass("text-warn");
    expect(screen.getAllByText("—")).toHaveLength(2);
    expect(screen.queryByText("GPU memory")).toBeNull();
  });

  it("reads several GPUs one value each, with their memory and a chart per reading", () => {
    const gpu = (i: number, over: object) =>
      ({
        index: i, gpu: String(i), uuid: `u${i}`, name: `G${i}`, memory_total: 100, memory_used: 50, memory_free: 50,
        memory_supported: true, temperature: 40, utilization: 10 * (i + 1), power_draw: 50, power_limit: null, ...over,
      }) as never;
    const two = stats({ gpu: [gpu(0, {}), gpu(1, { temperature: null })] });
    const history = {
      "n|u0": [
        { label: "GPU utilization", unit: "%", color: "red", samples: [] },
        { label: "Temperature", unit: "°C", color: "blue", samples: [] },
      ],
    };
    render(
      <>
        <NodeStatStrip node={NODE} interfaces="" stats={two} layout={null} loading={false} />
        <NodeCharts stats={two} history={history} />
      </>,
    );
    expect(screen.getByText("10% · 20%")).toBeInTheDocument();
    expect(screen.getByText("50 W · 50 W")).toBeInTheDocument();
    expect(screen.getByText("40°C")).toBeInTheDocument();
    // Two charts for the first GPU, each naming it because there are two.
    expect(screen.getAllByRole("heading", { name: "GPU utilization" })).toHaveLength(1);
    expect(screen.getAllByText("G0")).toHaveLength(2);
  });

  it("names each run's share of the memory under the bar", () => {
    const layout = {
      totalMb: 1000, usedMb: 400, freeMb: 600,
      segments: [
        { kind: "run", id: "d1", label: "qwen", mb: 300, color: "var(--run-1)" },
        { kind: "system", id: "", label: "", mb: 100, color: "var(--color-line)" },
      ],
    } as never;
    render(<NodeStatStrip node={NODE} interfaces="" stats={stats()} layout={layout} loading={false} />);
    expect(screen.getByRole("listitem")).toHaveTextContent("qwen");
  });

  it("draws a chart once there are samples, with no label of its own", () => {
    const one = stats({
      gpu: [{ index: 0, gpu: "0", uuid: "u", name: "G", memory_supported: false, temperature: 1, utilization: 1 } as never],
    });
    const samples = [
      { t: 0, value: 1 },
      { t: 5000, value: 3 },
    ];
    render(
      <NodeCharts stats={one} history={{ "n|u": [{ label: "GPU utilization", unit: "%", color: "red", samples }] }} />,
    );
    expect(screen.getByRole("img", { name: /^GPU utilization/ })).toBeInTheDocument();
    expect(screen.getByText(/now 3%/)).toBeInTheDocument();
  });

  it("renders nothing for an unreachable node's charts, processes and storage", () => {
    const { container } = render(
      <>
        <NodeCharts stats={stats({ reachable: false })} history={{}} />
        <NodeProcesses stats={stats({ reachable: false })} onChanged={() => {}} />
        <NodeStorage stats={stats({ reachable: false })} />
      </>,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("says a tracked process belongs to its run, and reports a kill that throws", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    vi.mocked(killGpuProcess).mockRejectedValue(new Error("agent gone"));
    render(
      <NodeProcesses
        stats={stats({
          name: "",
          address: "",
          processes: [{ gpu_uuid: "g", pid: 7, process_name: "vllm", used_memory: 10, is_tracked: true, deployment: "run-1" } as never],
        })}
        onChanged={onChanged}
      />,
    );
    const row = screen.getByTestId("gpu-process-n-7");
    expect(row).toHaveTextContent("run-1");
    await user.click(within(row).getByRole("button", { name: /^Kill / }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Kill process" }));
    expect(await screen.findByText("agent gone")).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("agent gone")).toBeNull());
    await user.click(within(row).getByRole("button", { name: /^Kill / }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("asks the nodes again after a kill that worked", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    vi.mocked(killGpuProcess).mockResolvedValue({ killed: true } as never);
    render(
      <NodeProcesses
        stats={stats({ processes: [{ gpu_uuid: "g", pid: 8, process_name: "x", used_memory: 1, is_tracked: true } as never] })}
        onChanged={onChanged}
      />,
    );
    await user.click(screen.getByRole("button", { name: /^Kill / }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Kill process" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });
});
