import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import HealthBadge, {
  HealthAlert,
  HealthHistoryChart,
  HealthMonitorControls,
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

describe("HealthAlert", () => {
  it("renders deployment health info", () => {
    render(
      <HealthAlert
        health={{
          deployment_id: "test-deployment",
          status: HealthStatus.HEALTHY,
          errors: [],
          warnings: [],
          restart_count: 0,
          gpu_errors: 0,
          last_check: new Date().toISOString(),
        }}
      />
    );
    expect(screen.getByText("test-deployment")).toBeInTheDocument();
    expect(screen.getByText("Healthy")).toBeInTheDocument();
  });

  it("shows errors when present", () => {
    render(
      <HealthAlert
        health={{
          deployment_id: "test-deployment",
          status: HealthStatus.UNHEALTHY,
          errors: ["GPU memory exceeded", "Container crashed"],
          warnings: [],
          restart_count: 2,
          gpu_errors: 1,
          last_check: new Date().toISOString(),
        }}
      />
    );
    expect(screen.getByText("• GPU memory exceeded")).toBeInTheDocument();
    expect(screen.getByText("• Container crashed")).toBeInTheDocument();
  });

  it("shows warnings when present", () => {
    render(
      <HealthAlert
        health={{
          deployment_id: "test-deployment",
          status: HealthStatus.DEGRADED,
          errors: [],
          warnings: ["High memory usage"],
          restart_count: 0,
          gpu_errors: 0,
          last_check: new Date().toISOString(),
        }}
      />
    );
    expect(screen.getByText("• High memory usage")).toBeInTheDocument();
  });

  it("shows restart count and GPU errors", () => {
    render(
      <HealthAlert
        health={{
          deployment_id: "test-deployment",
          status: HealthStatus.HEALTHY,
          errors: [],
          warnings: [],
          restart_count: 3,
          gpu_errors: 2,
          last_check: new Date().toISOString(),
        }}
      />
    );
    expect(screen.getByText("Restarts: 3")).toBeInTheDocument();
    expect(screen.getByText("GPU Errors: 2")).toBeInTheDocument();
  });
});

describe("HealthMonitorControls", () => {
  it("renders toggle controls", () => {
    const onToggle = vi.fn();
    render(<HealthMonitorControls isMonitoring={false} onToggle={onToggle} />);
    expect(screen.getByText("Health Monitoring:")).toBeInTheDocument();
    expect(screen.getByText("Inactive")).toBeInTheDocument();
  });

  it("shows active state when monitoring", () => {
    render(<HealthMonitorControls isMonitoring={true} onToggle={() => {}} />);
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("calls onToggle when button is clicked", () => {
    const onToggle = vi.fn();
    render(<HealthMonitorControls isMonitoring={false} onToggle={onToggle} />);
    const button = screen.getByRole("button", { hidden: true });
    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});

// ── HealthHistoryChart ───────────────────────────────────────────────────────
//
// Nothing in this system stores a health series: `DeploymentHealth` is a
// snapshot and the monitor broadcasts rather than records. The chart therefore
// draws only samples a caller has accumulated from a live stream, and its most
// important property is that it never manufactures one — an empty or one-point
// series is a sentence, not a line.

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
