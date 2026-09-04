/** Monitoring: GPU, CPU and disk, and the processes holding the GPU.
 *
 * Two things here are specific to this hardware rather than incidental. A GB10
 * reports `[N/A]` for GPU memory because host and device memory are unified,
 * so `memory_supported: false` has to render as an explanation rather than as
 * "0 / 0 MB" or an empty bar. And the process table exists to answer "what is
 * holding the GPU that I did not start" — which is why an untracked process is
 * marked as untracked and can be killed from here.
 *
 * The page also prefers the live SSE frame over the polled fetch, so a stream
 * that has started must win; otherwise the numbers freeze at whatever the
 * first fetch said.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import MemoryPage from "@/pages/MemoryPage";
import type { MemoryResponse } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchMemory: vi.fn(),
  killGpuProcess: vi.fn(),
  connectMetricsStream: vi.fn(),
}));

import { connectMetricsStream, fetchMemory, killGpuProcess } from "@/lib/api";

const UUID = "GPU-11111111-2222-3333-4444-555555555555";

function memory(over: Partial<MemoryResponse> = {}): MemoryResponse {
  return {
    gpu: [
      {
        index: 0,
        gpu: "GPU 0",
        uuid: UUID,
        name: "NVIDIA GB10",
        memory_total: 0,
        memory_used: 0,
        memory_free: 0,
        memory_supported: false,
        temperature: 47,
        utilization: 12,
        power_draw: null,
        power_limit: null,
      },
    ],
    cpu: { total: 131072, used: 43520, free: 87552, available: 92160, usage_percent: 33.2 },
    disk: [{ mount: "/", total: 1_290_277_824_000, used: 837_702_287_360, free: 452_575_536_640, usage_percent: 64.9 }],
    processes: [
      { gpu_uuid: UUID, pid: 98251, process_name: "VLLM::EngineCore", used_memory: 83421, is_tracked: false },
    ],
    ...over,
  };
}

/** The callback the page hands `connectMetricsStream`, so a test can push a frame. */
let emit: (event: string, data: unknown) => void;
const stopStream = vi.fn();

describe("MemoryPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchMemory).mockResolvedValue(memory());
    vi.mocked(killGpuProcess).mockResolvedValue({ killed: true, pid: 98251 });
    vi.mocked(connectMetricsStream).mockImplementation((onMessage) => {
      emit = onMessage;
      return stopStream;
    });
  });

  it("explains unified memory rather than drawing an empty usage bar", async () => {
    render(<MemoryPage />);

    expect(await screen.findByText("NVIDIA GB10")).toBeInTheDocument();
    expect(
      screen.getByText("Unified memory — usage not reported by nvidia-smi"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/0 \/ 0 MB/)).toBeNull();
    // The figures nvidia-smi *does* report on a GB10 are still shown.
    expect(screen.getAllByText("12%").length).toBeGreaterThan(0);
    expect(screen.getAllByText("47°C").length).toBeGreaterThan(0);
  });

  it("shows the memory bar for a GPU that does report its usage", async () => {
    vi.mocked(fetchMemory).mockResolvedValue(
      memory({
        gpu: [
          {
            index: 0,
            gpu: "GPU 0",
            uuid: UUID,
            name: "NVIDIA A100",
            memory_total: 81920,
            memory_used: 40960,
            memory_free: 40960,
            memory_supported: true,
            temperature: 88,
            utilization: 99,
            power_draw: 300,
            power_limit: 400,
          },
        ],
      }),
    );
    render(<MemoryPage />);

    expect(await screen.findByText("40960 / 81920 MB")).toBeInTheDocument();
    expect(screen.getByText("50.0%")).toBeInTheDocument();
    expect(screen.getByText("40960 MB free")).toBeInTheDocument();
  });

  it("marks a process nothing here started, and kills it on request", async () => {
    const user = userEvent.setup();
    render(<MemoryPage />);

    const row = (await screen.findByText("98251")).closest("tr")!;
    expect(within(row).getByText("untracked")).toBeInTheDocument();
    expect(within(row).getByText("83421 MB")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: /kill/i }));

    await waitFor(() => expect(killGpuProcess).toHaveBeenCalledWith(98251));
    // The polled figures are re-read, so the table cannot keep showing a
    // process that is gone.
    await waitFor(() => expect(fetchMemory).toHaveBeenCalledTimes(2));
  });

  it("prefers a live metrics frame over the figures it first polled", async () => {
    render(<MemoryPage />);
    await screen.findByText("NVIDIA GB10");

    act(() => {
      emit("metrics", memory({ cpu: { total: 131072, used: 65536, free: 65536, available: 65536, usage_percent: 50 } }));
    });

    expect(await screen.findByText("64.0 / 128.0 GB")).toBeInTheDocument();
  });

  it("ignores stream frames that are not metrics", async () => {
    render(<MemoryPage />);
    await screen.findByText("NVIDIA GB10");

    act(() => emit("error", { message: "stream hiccup" }));

    // Still the polled figures, not a blank page.
    expect(screen.getByText("NVIDIA GB10")).toBeInTheDocument();
  });

  it("closes the metrics stream when the page goes away", async () => {
    const { unmount } = render(<MemoryPage />);
    await screen.findByText("NVIDIA GB10");

    unmount();

    expect(stopStream).toHaveBeenCalled();
  });

  it("surfaces a failed read instead of an empty dashboard", async () => {
    vi.mocked(fetchMemory).mockRejectedValue(new Error("nvidia-smi not found"));
    render(<MemoryPage />);

    expect(await screen.findByText("nvidia-smi not found")).toBeInTheDocument();
    expect(await screen.findByText("No data available.")).toBeInTheDocument();
  });

  it("renders the disk card even on a machine reporting no GPU at all", async () => {
    vi.mocked(fetchMemory).mockResolvedValue(memory({ gpu: [], processes: [] }));
    render(<MemoryPage />);

    expect(await screen.findByText("/")).toBeInTheDocument();
    expect(screen.getByText("64.9%")).toBeInTheDocument();
    expect(screen.getByText("CPU Memory")).toBeInTheDocument();
    expect(screen.queryByText("GPU Processes")).toBeNull();
  });
});
