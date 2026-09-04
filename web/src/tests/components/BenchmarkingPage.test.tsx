/** The benchmarking page: runs, the summary table, and comparing two of them.
 *
 * A benchmark number is only worth anything next to another one, so the
 * behaviour that matters is the comparison path — selecting runs, asking the
 * backend to diff them, and rendering which way each metric moved — plus the
 * empty states, because a page that shows nothing and says nothing reads as
 * broken.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import BenchmarkingPage from "@/pages/BenchmarkingPage";
import type { BenchmarkResult } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchBenchmarks: vi.fn(),
  fetchLatestByRecipe: vi.fn(),
  runBenchmark: vi.fn(),
  compareRuns: vi.fn(),
}));

import { compareRuns, fetchBenchmarks, fetchLatestByRecipe, runBenchmark } from "@/lib/api";

const run = (over: Partial<BenchmarkResult> = {}): BenchmarkResult => ({
  benchmark_id: "bench-0001",
  deployment_id: "dep-1",
  recipe_id: "bundled/qwen3-8b",
  recipe_name: "Qwen3 8B",
  baseline_id: null,
  status: "completed",
  started_at: "2026-01-01T10:00:00Z",
  completed_at: "2026-01-01T10:05:00Z",
  params: {},
  results: { throughput: 1234.5, latency_ms: 42.25 },
  ...over,
});

const RUNS = [
  run(),
  run({
    benchmark_id: "bench-0002",
    recipe_name: "Qwen3 32B",
    baseline_id: "bench-0001",
  }),
];

const COMPARISON = {
  run_ids: ["bench-0001", "bench-0002"],
  runs: { "bench-0001": RUNS[0], "bench-0002": RUNS[1] },
  comparison: {
    throughput: {
      values: {
        "bench-0001": { value: 1234.5 },
        "bench-0002": { value: 1500 },
      },
      differences: {
        "bench-0002_vs_bench-0001": { difference_pct: 21.5 },
      },
    },
    latency_ms: {
      values: { "bench-0001": { value: 42.25 } },
      differences: { "bench-0001_vs_bench-0002": { difference_pct: -8 } },
    },
  },
};

describe("BenchmarkingPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchBenchmarks).mockResolvedValue(RUNS);
    vi.mocked(fetchLatestByRecipe).mockResolvedValue({});
    vi.mocked(runBenchmark).mockResolvedValue({} as never);
    vi.mocked(compareRuns).mockResolvedValue(COMPARISON as never);
  });

  it("lists every run with its recipe and status, counting them on the tab", async () => {
    render(<BenchmarkingPage />);

    expect(await screen.findByText("Qwen3 8B")).toBeInTheDocument();
    expect(screen.getByText("Qwen3 32B")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /History/ })).toHaveTextContent("(2)");
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
  });

  /** A run measured against a baseline is a different claim from a run
   *  measured on its own, and the list is where that distinction survives. */
  it("marks the run that was measured against a baseline", async () => {
    render(<BenchmarkingPage />);
    expect(await screen.findByText("vs baseline")).toBeInTheDocument();
  });

  it("says the page is empty rather than showing an empty list", async () => {
    vi.mocked(fetchBenchmarks).mockResolvedValue([]);
    render(<BenchmarkingPage />);

    expect(await screen.findByText("No benchmarks run yet.")).toBeInTheDocument();
  });

  it("surfaces a history the backend could not produce", async () => {
    vi.mocked(fetchBenchmarks).mockRejectedValue(new Error("benchmark store unreadable"));
    render(<BenchmarkingPage />);

    expect(await screen.findByText("benchmark store unreadable")).toBeInTheDocument();
  });

  describe("comparison", () => {
    /** One run selected is not a comparison, so the affordance stays hidden
     *  until there is something to compare it against. */
    it("offers nothing to compare until a second run is selected", async () => {
      render(<BenchmarkingPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      expect(screen.queryByRole("button", { name: /Compare Selected/ })).not.toBeInTheDocument();

      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      expect(screen.getByRole("button", { name: /Compare Selected/ })).toBeInTheDocument();
      expect(screen.getByText("2 run(s) selected")).toBeInTheDocument();
    });

    it("shows each metric side by side, and which way it moved", async () => {
      render(<BenchmarkingPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      await userEvent.click(screen.getByRole("button", { name: /Compare Selected/ }));

      await waitFor(() =>
        expect(compareRuns).toHaveBeenCalledWith(["bench-0001", "bench-0002"]),
      );
      expect(screen.getByRole("heading", { name: /Run Comparison/ })).toBeInTheDocument();
      expect(screen.getByText("throughput")).toBeInTheDocument();
      // The metric name loses its underscores; the numbers keep two decimals.
      expect(screen.getByText("latency ms")).toBeInTheDocument();
      expect(screen.getByText("1234.50")).toBeInTheDocument();
      expect(screen.getByText("21.5%")).toBeInTheDocument();
      expect(screen.getByText("8.0%")).toBeInTheDocument();
    });

    it("says so when the backend cannot diff the runs", async () => {
      vi.mocked(compareRuns).mockRejectedValue(new Error("nope"));
      render(<BenchmarkingPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      await userEvent.click(screen.getByRole("button", { name: /Compare Selected/ }));

      expect(await screen.findByText("Failed to compare benchmarks")).toBeInTheDocument();
    });

    it("clears the selection when the operator asks", async () => {
      render(<BenchmarkingPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      await userEvent.click(screen.getByRole("button", { name: "Clear" }));

      expect(screen.queryByText("2 run(s) selected")).not.toBeInTheDocument();
      expect(screen.getAllByRole("checkbox")[0]).not.toBeChecked();
    });

    it("deselects a run the operator ticked by mistake", async () => {
      render(<BenchmarkingPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[0]);

      expect(screen.getAllByRole("checkbox")[0]).not.toBeChecked();
    });

    it("closes the comparison and goes back to the history", async () => {
      render(<BenchmarkingPage />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      await userEvent.click(screen.getByRole("button", { name: /Compare Selected/ }));
      await screen.findByRole("heading", { name: /Run Comparison/ });

      await userEvent.click(screen.getByRole("button", { name: "Comparison" }));

      expect(screen.queryByRole("heading", { name: /Run Comparison/ })).not.toBeInTheDocument();
    });
  });

  describe("summary tab", () => {
    it("lays every recipe's latest numbers out in one table", async () => {
      vi.mocked(fetchLatestByRecipe).mockResolvedValue({
        "bundled/qwen3-8b": run({
          results: {
            throughput: 1234.5,
            latency_ms: 42.25,
            decode_latency_ms: 12.5,
            gpu_memory_gb: 60.2,
            gpu_utilization: 88.6,
            prefill_speed: 900.1,
          },
        }),
      });
      render(<BenchmarkingPage />);

      await userEvent.click(await screen.findByRole("button", { name: /Summary/ }));

      expect(screen.getByRole("columnheader", { name: "Throughput" })).toBeInTheDocument();
      expect(screen.getByText("1234.5")).toBeInTheDocument();
      expect(screen.getByText("42.3")).toBeInTheDocument();
      expect(screen.getByText("89%")).toBeInTheDocument();
    });

    /** A recipe benchmarked before a metric existed has no value for it, and
     *  an em dash is the honest rendering of that — not a zero. */
    it("writes an em dash for a metric a run never measured", async () => {
      vi.mocked(fetchLatestByRecipe).mockResolvedValue({
        "bundled/qwen3-8b": run({ results: null, recipe_name: "" }),
      });
      render(<BenchmarkingPage />);

      await userEvent.click(await screen.findByRole("button", { name: /Summary/ }));

      expect(screen.getByText("bundled/qwen3-8b")).toBeInTheDocument();
      expect(screen.getAllByText("—").length).toBe(6);
    });

    it("says the summary is empty rather than showing an empty table", async () => {
      render(<BenchmarkingPage />);
      await userEvent.click(await screen.findByRole("button", { name: /Summary/ }));

      expect(screen.getByText("No benchmark data yet.")).toBeInTheDocument();
    });
  });

  describe("running one", () => {
    it("will not start without a deployment to point at", async () => {
      render(<BenchmarkingPage />);
      await userEvent.click(screen.getByRole("button", { name: /Run Benchmark/ }));

      expect(screen.getByRole("button", { name: "Run" })).toBeDisabled();
    });

    it("sends the deployment, recipe, baseline and the chosen metrics", async () => {
      render(<BenchmarkingPage />);
      await userEvent.click(screen.getByRole("button", { name: /Run Benchmark/ }));

      await userEvent.type(screen.getByPlaceholderText("deployment-id"), "dep-42");
      await userEvent.type(screen.getByPlaceholderText("qwen3.5-397b-int4"), "bundled/qwen3-8b");
      await userEvent.type(screen.getByPlaceholderText("benchmark-id"), "bench-0001");
      await userEvent.click(screen.getByRole("checkbox", { name: "gpu memory" }));
      await userEvent.click(screen.getByRole("button", { name: "Run" }));

      await waitFor(() => expect(runBenchmark).toHaveBeenCalled());
      expect(vi.mocked(runBenchmark).mock.calls[0][0]).toEqual({
        deployment_id: "dep-42",
        baseline_id: "bench-0001",
        recipe_id: "bundled/qwen3-8b",
        recipe_name: "bundled/qwen3-8b",
        params: {
          benchmarks: ["throughput", "latency", "gpu_memory"],
          context_length: 4096,
        },
      });
    });

    it("drops a metric the operator unticks, and carries the context length", async () => {
      render(<BenchmarkingPage />);
      await userEvent.click(screen.getByRole("button", { name: /Run Benchmark/ }));

      await userEvent.type(screen.getByPlaceholderText("deployment-id"), "dep-42");
      await userEvent.click(screen.getByRole("checkbox", { name: "latency" }));
      // One change event, the way a paste or a spinner arrives: the field
      // falls back to 4096 whenever it is momentarily empty, so clearing it
      // and typing would leave "40968192" behind.
      fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "8192" } });
      await userEvent.click(screen.getByRole("button", { name: "Run" }));

      await waitFor(() => expect(runBenchmark).toHaveBeenCalled());
      expect(vi.mocked(runBenchmark).mock.calls[0][0].params).toEqual({
        benchmarks: ["throughput"],
        context_length: 8192,
      });
    });

    it("keeps the form open and says why when the run is refused", async () => {
      vi.mocked(runBenchmark).mockRejectedValue(new Error("deployment dep-42 is not running"));
      render(<BenchmarkingPage />);
      await userEvent.click(screen.getByRole("button", { name: /Run Benchmark/ }));

      await userEvent.type(screen.getByPlaceholderText("deployment-id"), "dep-42");
      await userEvent.click(screen.getByRole("button", { name: "Run" }));

      expect(await screen.findByText("deployment dep-42 is not running")).toBeInTheDocument();
      expect(screen.getByPlaceholderText("deployment-id")).toBeInTheDocument();
    });

    it("closes the form when the operator backs out", async () => {
      render(<BenchmarkingPage />);
      await userEvent.click(screen.getByRole("button", { name: /Run Benchmark/ }));
      await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByPlaceholderText("deployment-id")).not.toBeInTheDocument();
      expect(runBenchmark).not.toHaveBeenCalled();
    });
  });
});
