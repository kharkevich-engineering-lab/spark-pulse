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

  it("marks a process nothing here started, and kills it once confirmed", async () => {
    const user = userEvent.setup();
    render(<MemoryPage />);

    const row = (await screen.findByText("98251")).closest("tr")!;
    expect(within(row).getByText("untracked")).toBeInTheDocument();
    expect(within(row).getByText("83421 MB")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: /kill/i }));

    // Nothing is signalled on the click alone — the operator is asked first.
    expect(killGpuProcess).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Kill process" }));

    await waitFor(() => expect(killGpuProcess).toHaveBeenCalledWith(98251));
    // The polled figures are re-read, so the table cannot keep showing a
    // process that is gone.
    await waitFor(() => expect(fetchMemory).toHaveBeenCalledTimes(2));
  });

  // A misclick on this button ends somebody's inference run, so the dialog has
  // to name what dies in the terms the row already showed: pid, process, and
  // the memory it is holding.
  it("names the process, its pid and its memory before killing anything", async () => {
    const user = userEvent.setup();
    render(<MemoryPage />);

    const row = (await screen.findByText("98251")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: /kill/i }));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/PID 98251/)).toBeInTheDocument();
    expect(within(dialog).getByText(/VLLM::EngineCore/)).toBeInTheDocument();
    expect(within(dialog).getByText(/83421 MB/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Nothing on this page started it/)).toBeInTheDocument();
  });

  it("kills nothing when the confirmation is dismissed", async () => {
    const user = userEvent.setup();
    render(<MemoryPage />);

    const row = (await screen.findByText("98251")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: /kill/i }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(killGpuProcess).not.toHaveBeenCalled();
  });

  // `DELETE /api/memory/processes/{pid}` answers 200 with `{killed: false}`
  // when the signal did not land — a refusal and a success are the same HTTP
  // status, so a page that ignores the body reports a kill that never happened.
  it("says so when the backend refuses the kill", async () => {
    vi.mocked(killGpuProcess).mockResolvedValue({
      killed: false,
      pid: 98251,
      error: "Operation not permitted",
    });
    const user = userEvent.setup();
    render(<MemoryPage />);

    const row = (await screen.findByText("98251")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: /kill/i }));
    await user.click(screen.getByRole("button", { name: "Kill process" }));

    expect(await screen.findByText("PID 98251 was not killed")).toBeInTheDocument();
    expect(screen.getByText("Operation not permitted")).toBeInTheDocument();
  });

  it("says so when the kill fails without a reason", async () => {
    vi.mocked(killGpuProcess).mockResolvedValue({ killed: false, pid: 98251 });
    const user = userEvent.setup();
    render(<MemoryPage />);

    const row = (await screen.findByText("98251")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: /kill/i }));
    await user.click(screen.getByRole("button", { name: "Kill process" }));

    expect(await screen.findByText("PID 98251 was not killed")).toBeInTheDocument();
    expect(screen.getByText(/gave no reason/)).toBeInTheDocument();
  });

  it("surfaces a kill request that never reached the backend", async () => {
    vi.mocked(killGpuProcess).mockRejectedValue(new Error("API 500: nvidia-smi gone"));
    const user = userEvent.setup();
    render(<MemoryPage />);

    const row = (await screen.findByText("98251")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: /kill/i }));
    await user.click(screen.getByRole("button", { name: "Kill process" }));

    expect(await screen.findByText("Could not kill PID 98251")).toBeInTheDocument();
    expect(screen.getByText("API 500: nvidia-smi gone")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "OK" }));
    expect(screen.queryByText("Could not kill PID 98251")).toBeNull();
  });

  it("reports a tracked process as one of ours rather than a stranger's", async () => {
    vi.mocked(fetchMemory).mockResolvedValue(
      memory({
        processes: [
          { gpu_uuid: UUID, pid: 4242, process_name: "python", used_memory: 512, is_tracked: true },
        ],
      }),
    );
    const user = userEvent.setup();
    render(<MemoryPage />);

    const row = (await screen.findByText("4242")).closest("tr")!;
    await user.click(within(row).getByRole("button", { name: /kill/i }));

    expect(
      within(screen.getByRole("dialog")).getByText(/belongs to a deployment this page is tracking/),
    ).toBeInTheDocument();
  });

  // ── Live history ───────────────────────────────────────────────────────────
  //
  // Nothing in this system stores a health series, so the page draws only what
  // it has watched go past on the metrics stream. One reading is not a line,
  // and inventing the second one would be worse than saying so.

  it("says it has no history yet rather than drawing a line through one point", async () => {
    render(<MemoryPage />);

    // The count lands a render after the card does, so wait on the count.
    expect(await screen.findByText(/1 sample so far/)).toBeInTheDocument();
    expect(screen.getByText("Not enough history yet")).toBeInTheDocument();
  });

  it("draws utilization and temperature once the stream has reported twice", async () => {
    render(<MemoryPage />);
    await screen.findByText("NVIDIA GB10");

    const frame = (utilization: number, temperature: number) =>
      memory({
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
            temperature,
            utilization,
            power_draw: null,
            power_limit: null,
          },
        ],
      });

    act(() => emit("metrics", frame(30, 50)));
    act(() => emit("metrics", frame(60, 55)));

    // Three real readings: the polled one plus the two frames.
    // One per series: utilization and temperature.
    expect(await screen.findAllByText(/3 samples over the last/)).toHaveLength(2);
    expect(screen.getByText("Live history")).toBeInTheDocument();
    expect(screen.getByText(/now 60% . low 12% . peak 60%/)).toBeInTheDocument();
    expect(screen.getByText(/now 55°C . low 47°C . peak 55°C/)).toBeInTheDocument();
    // And it says the series is not kept.
    expect(screen.getByText(/starts over on reload/)).toBeInTheDocument();
    expect(screen.queryByText("Not enough history yet")).toBeNull();
  });

  it("keeps one GPU's history apart from another's", async () => {
    const OTHER = "GPU-99999999-8888-7777-6666-555555555555";
    const two = (utilization: number): MemoryResponse => ({
      ...memory(),
      gpu: [
        ...memory().gpu,
        {
          index: 1,
          gpu: "GPU 1",
          uuid: OTHER,
          name: "NVIDIA A100",
          memory_total: 100,
          memory_used: 50,
          memory_free: 50,
          memory_supported: true,
          temperature: 70,
          utilization,
          power_draw: null,
          power_limit: null,
        },
      ],
    });
    vi.mocked(fetchMemory).mockResolvedValue(two(90));
    render(<MemoryPage />);
    await screen.findByText("NVIDIA A100");

    act(() => emit("metrics", two(80)));

    // Each card carries its own series, scaled to its own readings.
    expect(await screen.findByText(/now 80% . low 80% . peak 90%/)).toBeInTheDocument();
    expect(screen.getByText(/now 12% . low 12% . peak 12%/)).toBeInTheDocument();
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
