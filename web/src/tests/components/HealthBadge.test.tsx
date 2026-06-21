import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import HealthBadge, { HealthAlert, HealthMonitorControls } from "@/components/HealthBadge";
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
