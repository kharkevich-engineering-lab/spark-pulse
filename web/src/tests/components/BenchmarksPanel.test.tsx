/** The benchmarks tab: runs, the summary, and comparing two of them.
 *
 * A benchmark number is only worth anything next to another one, so the
 * behaviour that matters is the comparison path — selecting runs, asking the
 * backend to diff them, and rendering which way each metric moved — plus the
 * empty states, because a panel that shows nothing and says nothing reads as
 * broken. Starting a run is not here any more: the launcher opens from the run
 * it measures, and its tests are on the Runs page.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import BenchmarksPanel from "@/components/BenchmarksPanel";
import { useQuery } from "@/hooks/useQuery";
import type { BenchmarkResult } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchBenchmarks: vi.fn(),
  fetchLatestByRecipe: vi.fn(),
  compareRuns: vi.fn(),
  deleteBenchmark: vi.fn(),
}));

import { compareRuns, deleteBenchmark, fetchBenchmarks, fetchLatestByRecipe } from "@/lib/api";

/** The panel is fed by its page. This is that wiring, so the tests can still
 *  assert that a delete re-reads the history rather than editing it in place. */
function Harness() {
  const { data, loading, error, refetch } = useQuery(fetchBenchmarks);
  return <BenchmarksPanel benchmarks={data} loading={loading} error={error} refetch={refetch} />;
}

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

describe("BenchmarksPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchBenchmarks).mockResolvedValue(RUNS);
    vi.mocked(fetchLatestByRecipe).mockResolvedValue({});
    vi.mocked(compareRuns).mockResolvedValue(COMPARISON as never);
    vi.mocked(deleteBenchmark).mockResolvedValue(undefined);
  });

  it("lists every run with its recipe and status, counting them on the tab", async () => {
    render(<Harness />);

    expect(await screen.findByText("Qwen3 8B")).toBeInTheDocument();
    expect(screen.getByText("Qwen3 32B")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /History/ })).toHaveTextContent("(2)");
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
  });

  /** An empty destination whose emptiness meant "you have not done something
   *  elsewhere yet" is not a tab. The comparison renders where the selection
   *  is made, so there are two pills rather than three. */
  it("offers two sub-views, not an empty comparison tab", async () => {
    render(<Harness />);
    await screen.findByText("Qwen3 8B");

    expect(screen.getAllByRole("tab")).toHaveLength(2);
    expect(screen.queryByRole("tab", { name: "Comparison" })).toBeNull();
  });

  /** A run measured against a baseline is a different claim from a run
   *  measured on its own, and the list is where that distinction survives. */
  it("marks the run that was measured against a baseline", async () => {
    render(<Harness />);
    expect(await screen.findByText("vs baseline")).toBeInTheDocument();
  });

  it("says the panel is empty rather than showing an empty list", async () => {
    vi.mocked(fetchBenchmarks).mockResolvedValue([]);
    render(<Harness />);

    expect(await screen.findByText("No benchmarks run yet.")).toBeInTheDocument();
  });

  it("surfaces a history the backend could not produce", async () => {
    vi.mocked(fetchBenchmarks).mockRejectedValue(new Error("benchmark store unreadable"));
    render(<Harness />);

    expect(await screen.findByText("benchmark store unreadable")).toBeInTheDocument();
  });

  /** A benchmark result is a record somebody made, and a bad run is worth
   *  less than nothing next to the good ones — so removing one has to be
   *  possible, deliberate, and honest about being refused. */
  describe("deleting a run", () => {
    const deleteButtons = () => screen.getAllByRole("button", { name: "Delete this run" });

    it("asks before removing anything, naming the run", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(deleteButtons()[0]);

      expect(screen.getByText("Delete benchmark run?")).toBeInTheDocument();
      expect(
        screen.getByText(/"Qwen3 8B" and its measurements are removed for good/),
      ).toBeInTheDocument();
      expect(deleteBenchmark).not.toHaveBeenCalled();
    });

    it("deletes the run it was pointed at and refetches the list", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 32B");
      expect(fetchBenchmarks).toHaveBeenCalledTimes(1);

      await userEvent.click(deleteButtons()[1]);
      await userEvent.click(screen.getByRole("button", { name: "Delete" }));

      await waitFor(() => expect(deleteBenchmark).toHaveBeenCalledWith("bench-0002"));
      // The list is re-read rather than edited in place: retention and other
      // operators both touch this store.
      await waitFor(() => expect(fetchBenchmarks).toHaveBeenCalledTimes(2));
      expect(screen.queryByText("Delete benchmark run?")).not.toBeInTheDocument();
    });

    it("leaves the run alone when the operator backs out", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(deleteButtons()[0]);
      await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

      expect(screen.queryByText("Delete benchmark run?")).not.toBeInTheDocument();
      expect(deleteBenchmark).not.toHaveBeenCalled();
    });

    /** The 409 the backend raises for a run still in progress is the one
     *  reason an operator most needs to read, so it has to reach the page. */
    it("says why a delete was refused", async () => {
      vi.mocked(deleteBenchmark).mockRejectedValue(
        new Error("API 409: benchmark bench-0001 is still running"),
      );
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(deleteButtons()[0]);
      await userEvent.click(screen.getByRole("button", { name: "Delete" }));

      expect(
        await screen.findByText("API 409: benchmark bench-0001 is still running"),
      ).toBeInTheDocument();
      expect(fetchBenchmarks).toHaveBeenCalledTimes(1);
    });

    it("says so when the run had already gone", async () => {
      vi.mocked(deleteBenchmark).mockRejectedValue(new Error("API 404: Benchmark not found"));
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(deleteButtons()[0]);
      await userEvent.click(screen.getByRole("button", { name: "Delete" }));

      expect(await screen.findByText("API 404: Benchmark not found")).toBeInTheDocument();
    });

    /** A deleted run left in the selection would send the compare call an id
     *  the backend no longer has, and it answers 404 for the whole set. */
    it("drops the deleted run from a pending comparison selection", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      expect(screen.getByText("2 runs selected")).toBeInTheDocument();

      await userEvent.click(deleteButtons()[0]);
      await userEvent.click(screen.getByRole("button", { name: "Delete" }));

      await waitFor(() => expect(deleteBenchmark).toHaveBeenCalledWith("bench-0001"));
      await waitFor(() =>
        expect(screen.queryByText("2 runs selected")).not.toBeInTheDocument(),
      );
    });
  });

  describe("comparison", () => {
    /** One run selected is not a comparison, so the affordance stays hidden
     *  until there is something to compare it against. */
    it("offers nothing to compare until a second run is selected", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      expect(screen.queryByRole("button", { name: /Compare selected/ })).not.toBeInTheDocument();

      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      expect(screen.getByRole("button", { name: /Compare selected/ })).toBeInTheDocument();
      expect(screen.getByText("2 runs selected")).toBeInTheDocument();
    });

    it("shows each metric side by side, and which way it moved", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      await userEvent.click(screen.getByRole("button", { name: /Compare selected/ }));

      await waitFor(() =>
        expect(compareRuns).toHaveBeenCalledWith(["bench-0001", "bench-0002"]),
      );
      const panel = within(screen.getByTestId("comparison"));
      expect(screen.getByRole("heading", { name: /Comparison/ })).toBeInTheDocument();
      expect(panel.getByText("throughput")).toBeInTheDocument();
      // The metric name loses its underscores; the numbers keep two decimals.
      expect(panel.getByText("latency ms")).toBeInTheDocument();
      expect(panel.getByText("1234.50")).toBeInTheDocument();
      expect(panel.getByText("21.5%")).toBeInTheDocument();
      expect(panel.getByText("8.0%")).toBeInTheDocument();
    });

    it("says so when the backend cannot diff the runs", async () => {
      vi.mocked(compareRuns).mockRejectedValue(new Error("nope"));
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      await userEvent.click(screen.getByRole("button", { name: /Compare selected/ }));

      expect(await screen.findByText("Failed to compare benchmarks")).toBeInTheDocument();
    });

    it("clears the selection when the operator asks", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      await userEvent.click(screen.getByRole("button", { name: "Clear" }));

      expect(screen.queryByText("2 runs selected")).not.toBeInTheDocument();
      expect(screen.getAllByRole("checkbox")[0]).not.toBeChecked();
    });

    it("deselects a run the operator ticked by mistake", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[0]);

      expect(screen.getAllByRole("checkbox")[0]).not.toBeChecked();
    });

    it("puts the comparison away again from its own close", async () => {
      render(<Harness />);
      await screen.findByText("Qwen3 8B");

      await userEvent.click(screen.getAllByRole("checkbox")[0]);
      await userEvent.click(screen.getAllByRole("checkbox")[1]);
      await userEvent.click(screen.getByRole("button", { name: /Compare selected/ }));
      await screen.findByTestId("comparison");

      await userEvent.click(within(screen.getByTestId("comparison")).getByTitle("Close"));

      expect(screen.queryByTestId("comparison")).not.toBeInTheDocument();
      // The history is still there underneath.
      expect(screen.getByText("Qwen3 8B")).toBeInTheDocument();
    });
  });

  describe("summary", () => {
    const LATEST = {
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
    };

    it("lays every recipe's latest numbers out in one table", async () => {
      vi.mocked(fetchLatestByRecipe).mockResolvedValue(LATEST);
      render(<Harness />);

      await userEvent.click(await screen.findByRole("tab", { name: /Summary/ }));

      const table = within(screen.getByTestId("summary-table"));
      expect(screen.getByRole("columnheader", { name: "Throughput" })).toBeInTheDocument();
      expect(table.getByText("1234.5")).toBeInTheDocument();
      expect(table.getByText("42.3")).toBeInTheDocument();
      expect(table.getByText("89%")).toBeInTheDocument();
    });

    /** Nine columns do not survive 390px however they are scrolled: the model
     *  name goes off screen with the numbers, so the reader is swiping a table
     *  with no row labels. Under 900 each recipe is a card, with the same
     *  numbers named. */
    it("repeats the same numbers as cards for a narrow screen", async () => {
      vi.mocked(fetchLatestByRecipe).mockResolvedValue(LATEST);
      render(<Harness />);

      await userEvent.click(await screen.findByRole("tab", { name: /Summary/ }));

      const cards = within(screen.getByTestId("summary-cards"));
      expect(cards.getByText("Qwen3 8B")).toBeInTheDocument();
      expect(cards.getByText("Throughput")).toBeInTheDocument();
      expect(cards.getByText("1234.5")).toBeInTheDocument();
      expect(cards.getByText("89%")).toBeInTheDocument();
    });

    /** A recipe benchmarked before a metric existed has no value for it, and
     *  an em dash is the honest rendering of that — not a zero. */
    it("writes an em dash for a metric a run never measured", async () => {
      vi.mocked(fetchLatestByRecipe).mockResolvedValue({
        "bundled/qwen3-8b": run({ results: null, recipe_name: "" }),
      });
      render(<Harness />);

      await userEvent.click(await screen.findByRole("tab", { name: /Summary/ }));

      const table = within(screen.getByTestId("summary-table"));
      expect(table.getByText("bundled/qwen3-8b")).toBeInTheDocument();
      expect(table.getAllByText("—").length).toBe(6);
    });

    it("says the summary is empty rather than showing an empty table", async () => {
      render(<Harness />);
      await userEvent.click(await screen.findByRole("tab", { name: /Summary/ }));

      expect(screen.getByText("No benchmark data yet.")).toBeInTheDocument();
    });
  });
});
