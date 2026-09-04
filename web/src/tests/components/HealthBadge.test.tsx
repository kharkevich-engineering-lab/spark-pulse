import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import HealthBadge, {
  HealthHistoryChart,
  medianInterval,
  sparklineBreaks,
  sparklineGapBands,
  sparklinePath,
  type HealthSample,
} from "@/components/HealthBadge";
import { HealthStatus } from "@/lib/operations";

describe("HealthBadge", () => {
  it("renders healthy status with label", () => {
    render(<HealthBadge status={HealthStatus.HEALTHY} />);
    expect(screen.getByText("Healthy")).toBeInTheDocument();
  });

  it("renders degraded status with label", () => {
    render(<HealthBadge status={HealthStatus.DEGRADED} />);
    expect(screen.getByText("Degraded")).toBeInTheDocument();
  });

  it("renders unhealthy status with label", () => {
    render(<HealthBadge status={HealthStatus.UNHEALTHY} />);
    expect(screen.getByText("Unhealthy")).toBeInTheDocument();
  });

  it("renders unknown status with label", () => {
    render(<HealthBadge status={HealthStatus.UNKNOWN} />);
    expect(screen.getByText("Unknown")).toBeInTheDocument();
  });

  it("hides label when showLabel is false", () => {
    render(<HealthBadge status={HealthStatus.HEALTHY} showLabel={false} />);
    expect(screen.queryByText("Healthy")).not.toBeInTheDocument();
  });

  it("applies size classes correctly", () => {
    const { container } = render(<HealthBadge status={HealthStatus.HEALTHY} size="sm" />);
    const dot = container.querySelector("span.rounded-full") as HTMLElement;
    expect(dot).toHaveClass("w-2", "h-2");
  });

  it("applies custom className", () => {
    const { container } = render(
      <HealthBadge status={HealthStatus.HEALTHY} className="custom-class" />
    );
    expect(container.firstChild).toHaveClass("custom-class");
  });
});

// ── HealthHistoryChart ───────────────────────────────────────────────────────
//
// Nothing in this system stores a long health series. The chart draws only
// samples a caller has accumulated from a live stream or read out of the
// backend's own bounded window, and its two most important properties are that
// it never manufactures a *point* — an empty or one-point series is a
// sentence, not a line — and that it never manufactures a *line*: the x axis is
// each sample's timestamp, so a silence is drawn as wide as it was long, and
// the stroke is cut across it.

function samples(values: number[], stepMs = 5000): HealthSample[] {
  return values.map((value, i) => ({ t: 1_700_000_000_000 + i * stepMs, value }));
}

const utilization = (values: number[]) => ({
  label: "GPU utilization",
  unit: "%",
  color: "var(--color-primary)",
  samples: samples(values),
});

describe("HealthHistoryChart", () => {
  it("says it has nothing rather than drawing an empty axis", () => {
    render(<HealthHistoryChart series={[utilization([])]} />);

    expect(screen.getByText("Not enough history yet")).toBeInTheDocument();
    expect(
      screen.getByText("Nothing has arrived on the stream since this page was opened."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("refuses to draw a line through a single reading, and says how many it has", () => {
    render(<HealthHistoryChart series={[utilization([42])]} />);

    expect(screen.getByText("Not enough history yet")).toBeInTheDocument();
    expect(screen.getByText("1 sample so far — two are needed to draw a line.")).toBeInTheDocument();
  });

  it("counts the samples it has when several series are still short", () => {
    render(
      <HealthHistoryChart
        series={[utilization([42]), { ...utilization([]), label: "Temperature", unit: "°C" }]}
      />,
    );

    expect(screen.getByText("1 sample so far — two are needed to draw a line.")).toBeInTheDocument();
  });

  it("draws a series once there are two readings, with its range", () => {
    render(<HealthHistoryChart series={[utilization([10, 90, 40])]} />);

    expect(screen.queryByText("Not enough history yet")).toBeNull();
    expect(screen.getByText("GPU utilization")).toBeInTheDocument();
    expect(screen.getByText(/now 40% . low 10% . peak 90%/)).toBeInTheDocument();
    expect(screen.getByText("3 samples over the last 10 s")).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /GPU utilization, 3 samples over the last 10 s, now 40%/ }),
    ).toBeInTheDocument();
  });

  it("draws only the series that have enough readings", () => {
    render(
      <HealthHistoryChart
        series={[
          utilization([10, 90]),
          { label: "Temperature", unit: "°C", color: "red", samples: samples([70]) },
        ]}
      />,
    );

    expect(screen.getByText("GPU utilization")).toBeInTheDocument();
    expect(screen.queryByText("Temperature")).toBeNull();
    expect(screen.queryByText("Not enough history yet")).toBeNull();
  });

  it("carries a title and a caption saying where the numbers came from", () => {
    render(
      <HealthHistoryChart
        series={[utilization([1, 2])]}
        title="Live history"
        caption="Sampled since this page was opened."
        className="mt-4"
      />,
    );

    expect(screen.getByRole("heading", { name: "Live history" })).toBeInTheDocument();
    expect(screen.getByText("Sampled since this page was opened.")).toBeInTheDocument();
  });

  it("rounds a fractional reading to one decimal rather than printing float noise", () => {
    render(<HealthHistoryChart series={[{ ...utilization([33.333333, 66.666666]), unit: "%" }]} />);

    expect(screen.getByText(/now 66.7% . low 33.3% . peak 66.7%/)).toBeInTheDocument();
  });

  it("reports the window it covers in minutes and in hours", () => {
    const { unmount } = render(
      <HealthHistoryChart series={[{ ...utilization([1, 2]), samples: samples([1, 2], 600_000) }]} />,
    );
    expect(screen.getByText("2 samples over the last 10 min")).toBeInTheDocument();
    unmount();

    render(
      <HealthHistoryChart series={[{ ...utilization([1, 2]), samples: samples([1, 2], 7_200_000) }]} />,
    );
    expect(screen.getByText("2 samples over the last 2 h")).toBeInTheDocument();
  });
});

describe("sparklinePath", () => {
  it("draws nothing for a series too short to be a line", () => {
    expect(sparklinePath([])).toBe("");
    expect(sparklinePath(samples([5]))).toBe("");
  });

  it("spans the full width and puts the lowest reading at the bottom", () => {
    const path = sparklinePath(samples([0, 100]));

    expect(path).toBe("M0.00,37.00 L300.00,3.00");
  });

  // A GPU parked at one utilisation figure has no range to scale against;
  // dividing by that zero span would put the whole series on the floor (or
  // produce NaN), so a flat series is drawn flat, down the middle.
  it("draws a series that never moved as a flat line down the middle", () => {
    expect(sparklinePath(samples([50, 50, 50]))).toBe("M0.00,20.00 L150.00,20.00 L300.00,20.00");
  });
});

// ── Time, not index ──────────────────────────────────────────────────────────
//
// The bug these guard: `x` used to be `i / (n - 1)`, so a ten-minute silence
// between two frames was drawn exactly as wide as a five-second one, with a
// straight line through it. `EventSource` reconnects silently, so that silence
// is a normal thing to have. The comment above the function claimed it "never
// invents a point" — true of the points, false of the line joining them.

/** Samples at explicit offsets in seconds from a fixed epoch. */
function at(offsetsSeconds: number[], values: number[]): HealthSample[] {
  return offsetsSeconds.map((s, i) => ({
    t: 1_700_000_000_000 + s * 1000,
    value: values[i],
  }));
}

describe("medianInterval", () => {
  it("has no interval to report from fewer than two samples", () => {
    expect(medianInterval([])).toBe(0);
    expect(medianInterval(samples([1]))).toBe(0);
  });

  it("is the middle interval of an odd number of them", () => {
    expect(medianInterval(at([0, 5, 10, 15, 600], [1, 2, 3, 4, 5]))).toBe(5000);
  });

  it("is the mean of the middle two of an even number", () => {
    // Five samples, four intervals: 5, 5, 20, 30 seconds.
    expect(medianInterval(at([0, 5, 10, 30, 60], [1, 2, 3, 4, 5]))).toBe(12_500);
  });

  // The mean would be dragged up by the silence until the silence no longer
  // looked unusual — the outlier would hide itself.
  it("is not moved by one long silence", () => {
    expect(medianInterval(at([0, 5, 10, 15, 20, 3000], [1, 2, 3, 4, 5, 6]))).toBe(5000);
  });
});

describe("sparklineBreaks", () => {
  it("finds nothing in an evenly sampled series", () => {
    expect(sparklineBreaks(samples([1, 2, 3, 4]))).toEqual([]);
  });

  it("does not call one dropped frame a gap", () => {
    expect(sparklineBreaks(at([0, 5, 15, 20, 25], [1, 2, 3, 4, 5]))).toEqual([]);
  });

  it("calls a silence far past the usual cadence a gap", () => {
    expect(sparklineBreaks(at([0, 5, 10, 610, 615], [1, 2, 3, 4, 5]))).toEqual([3]);
  });

  it("honours a break the caller declares, by timestamp", () => {
    const s = samples([1, 2, 3]);

    expect(sparklineBreaks(s, [s[2].t])).toEqual([2]);
  });

  it("has nothing to break in a series too short to draw", () => {
    expect(sparklineBreaks(samples([1]))).toEqual([]);
  });
});

describe("sparklinePath spaces the axis by time", () => {
  it("puts a sample at the fraction of the window it actually happened at", () => {
    // Three readings, the last a long time after the second. By index the
    // middle point would sit at x=150; by time it belongs near the left.
    const path = sparklinePath(at([0, 10, 100], [0, 50, 100]));
    const xs = path.split(" ").map((cmd) => Number(cmd.slice(1).split(",")[0]));

    expect(xs[0]).toBe(0);
    expect(xs[1]).toBeCloseTo(30, 1);
    expect(xs[2]).toBe(300);
  });

  it("draws no line across a gap: the stroke restarts on the far side", () => {
    const path = sparklinePath(at([0, 5, 10, 610, 615], [1, 2, 3, 4, 5]));

    // One M for the start, one for the resumption after the silence.
    expect(path.match(/M/g)).toHaveLength(2);
    expect(path.split(" ")[3].startsWith("M")).toBe(true);
  });

  it("draws no line into a break the caller declared", () => {
    const s = samples([1, 2, 3, 4]);

    const path = sparklinePath(s, [s[2].t]);

    expect(path.match(/M/g)).toHaveLength(2);
  });

  it("puts every sample at x=0 when they all share one instant", () => {
    const path = sparklinePath(at([0, 0, 0], [1, 2, 3]));

    expect(path.split(" ").every((cmd) => cmd.slice(1).startsWith("0.00,"))).toBe(true);
  });

  it("is unchanged for an evenly sampled series, which is the common case", () => {
    expect(sparklinePath(samples([50, 50, 50]))).toBe(
      "M0.00,20.00 L150.00,20.00 L300.00,20.00",
    );
  });
});

describe("sparklineGapBands", () => {
  it("has no bands when nothing was missed", () => {
    expect(sparklineGapBands(samples([1, 2, 3]))).toEqual([]);
    expect(sparklineGapBands(samples([1]))).toEqual([]);
  });

  it("spans the silence, so a long gap looks long", () => {
    const [band] = sparklineGapBands(at([0, 5, 10, 610, 615], [1, 2, 3, 4, 5]));

    expect(band.x).toBeCloseTo((10 / 615) * 300, 1);
    expect(band.width).toBeCloseTo((600 / 615) * 300, 1);
  });

  it("gives a declared break a visible width even when it spans one tick", () => {
    const s = samples([1, 2, 3]);

    const [band] = sparklineGapBands(s, [s[2].t]);

    expect(band.width).toBeGreaterThanOrEqual(2);
  });
});

describe("the chart shows a gap rather than hiding it", () => {
  const withGap = (offsets: number[], values: number[]) => ({
    ...utilization([]),
    samples: at(offsets, values),
  });

  it("shades the silence and counts it under the sparkline", () => {
    render(<HealthHistoryChart series={[withGap([0, 5, 10, 610, 615], [1, 2, 3, 4, 5])]} />);

    expect(screen.getAllByTestId("sparkline-gap")).toHaveLength(1);
    expect(screen.getByText(/1 gap with no measurement/)).toBeInTheDocument();
  });

  it("says so in the accessible label too", () => {
    render(<HealthHistoryChart series={[withGap([0, 5, 10, 610, 615], [1, 2, 3, 4, 5])]} />);

    expect(
      screen.getByRole("img", { name: /1 gap where nothing was measured/ }),
    ).toBeInTheDocument();
  });

  it("pluralises several gaps", () => {
    render(
      <HealthHistoryChart
        series={[withGap([0, 5, 10, 610, 615, 1300], [1, 2, 3, 4, 5, 6])]}
      />,
    );

    expect(screen.getAllByTestId("sparkline-gap")).toHaveLength(2);
    expect(screen.getByText(/2 gaps with no measurement/)).toBeInTheDocument();
  });

  it("says nothing about gaps when there are none", () => {
    render(<HealthHistoryChart series={[utilization([1, 2, 3])]} />);

    expect(screen.queryByTestId("sparkline-gap")).toBeNull();
    expect(screen.queryByText(/gap/)).toBeNull();
  });
});
