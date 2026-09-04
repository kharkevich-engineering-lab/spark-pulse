import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import DeploymentPipeline, { PipelineStep } from "@/components/DeploymentPipeline";
import { Play, ShieldCheck, Wifi } from "lucide-react";

const defaultSteps: PipelineStep[] = [
  { id: "1", label: "Resolve Script", status: "pending", icon: Play },
  { id: "2", label: "Validate Cluster", status: "pending", icon: ShieldCheck },
  { id: "3", label: "Start Head", status: "pending", icon: Play },
  { id: "4", label: "Start Workers", status: "pending", icon: Wifi },
];

describe("DeploymentPipeline", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the component with steps", () => {
    render(<DeploymentPipeline steps={defaultSteps} />);
    expect(screen.getByText("Deployment Pipeline")).toBeInTheDocument();
    expect(screen.getByText("0/4 steps completed")).toBeInTheDocument();
  });

  it("shows progress bar at 0%", () => {
    render(<DeploymentPipeline steps={defaultSteps} />);
    const progressBar = document.querySelector(".bg-primary") as HTMLElement;
    expect(progressBar).toHaveStyle({ width: "0%" });
  });

  it("shows running step with spinner", () => {
    const runningSteps = [
      { ...defaultSteps[0], status: "success" as const },
      { ...defaultSteps[1], status: "success" as const },
      { ...defaultSteps[2], status: "running" as const },
      defaultSteps[3],
    ];
    render(<DeploymentPipeline steps={runningSteps} activeStep={2} />);
    expect(screen.getByText("Running: Start Head")).toBeInTheDocument();
    expect(screen.getByText("2/4 steps completed")).toBeInTheDocument();
  });

  it("shows completed state when all steps succeed", () => {
    const completedSteps = defaultSteps.map((s) => ({ ...s, status: "success" as const }));
    render(<DeploymentPipeline steps={completedSteps} />);
    expect(screen.getByText("Deployment completed successfully")).toBeInTheDocument();
    expect(screen.getByText("4/4 steps completed")).toBeInTheDocument();
  });

  it("shows failed state with retry button", () => {
    const failedSteps = defaultSteps.map((s, i) =>
      i === 2 ? { ...s, status: "failed" as const, error: "Connection timeout" } : s
    );
    const onRetry = vi.fn();
    render(<DeploymentPipeline steps={failedSteps} onRetry={onRetry} />);

    expect(screen.getByText("Connection timeout")).toBeInTheDocument();
    expect(screen.getByText("Retry")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("shows cancel button when onCancel is provided", () => {
    const onCancel = vi.fn();
    render(<DeploymentPipeline steps={defaultSteps} onCancel={onCancel} />);
    expect(screen.getByText("Cancel")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Cancel"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("disables cancel button when deployment is complete", () => {
    const completedSteps = defaultSteps.map((s) => ({ ...s, status: "success" as const }));
    const onCancel = vi.fn();
    render(<DeploymentPipeline steps={completedSteps} onCancel={onCancel} />);
    expect(screen.getByText("Cancel")).toBeDisabled();
  });

  it("shows step duration when provided", () => {
    const stepsWithDuration = defaultSteps.map((s, i) =>
      i < 2 ? { ...s, status: "success" as const, duration: i + 5 } : s
    );
    render(<DeploymentPipeline steps={stepsWithDuration} />);
    expect(screen.getByText("5s")).toBeInTheDocument();
    expect(screen.getByText("6s")).toBeInTheDocument();
  });

  it("shows skipped steps with alert icon", () => {
    const stepsWithSkipped = [
      { ...defaultSteps[0], status: "success" as const },
      { ...defaultSteps[1], status: "success" as const },
      { ...defaultSteps[2], status: "skipped" as const },
      defaultSteps[3],
    ];
    render(<DeploymentPipeline steps={stepsWithSkipped} />);
    // Only "success" steps count as completed, not "skipped"
    expect(screen.getByText("2/4 steps completed")).toBeInTheDocument();
  });

  it("applies custom className", () => {
    const { container } = render(
      <DeploymentPipeline steps={defaultSteps} className="custom-class" />
    );
    expect(container.firstChild).toHaveClass("custom-class");
  });
});
