import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import LockContentionAlert, { LockStatusIndicator } from "@/components/LockContentionAlert";
import { LockType } from "@/lib/operations";

const mockLock = {
  lock_id: "test-lock-1",
  resource: "cluster-1",
  lock_type: LockType.CLUSTER_START,
  holder: "api",
  acquired_at: new Date().toISOString(),
};

describe("LockContentionAlert", () => {
  it("renders lock contention alert", () => {
    render(<LockContentionAlert lock={mockLock} />);
    expect(screen.getByText("Resource Locked")).toBeInTheDocument();
    expect(screen.getByText("cluster-1")).toBeInTheDocument();
  });

  it("shows lock type label", () => {
    render(<LockContentionAlert lock={mockLock} />);
    expect(screen.getByText("Cluster Start")).toBeInTheDocument();
  });

  it("shows holder when present", () => {
    render(<LockContentionAlert lock={mockLock} />);
    expect(screen.getByText("api")).toBeInTheDocument();
  });

  it("shows estimated wait time", () => {
    render(<LockContentionAlert lock={mockLock} estimatedWaitSeconds={30} />);
    expect(screen.getByText(/Est. wait: ~30s/)).toBeInTheDocument();
  });

  it("calls onRetry when button is clicked", () => {
    const onRetry = vi.fn();
    render(<LockContentionAlert lock={mockLock} onRetry={onRetry} />);
    fireEvent.click(screen.getByText("Try Again"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("applies custom className", () => {
    const { container } = render(
      <LockContentionAlert lock={mockLock} className="custom-class" />
    );
    expect(container.firstChild).toHaveClass("custom-class");
  });
});

describe("LockStatusIndicator", () => {
  it("renders lock status inline", () => {
    render(<LockStatusIndicator lock={mockLock} />);
    expect(screen.getByText("Cluster Start")).toBeInTheDocument();
  });

  it("shows holder when present", () => {
    render(<LockStatusIndicator lock={mockLock} />);
    expect(screen.getByText("by api")).toBeInTheDocument();
  });

  it("applies custom className", () => {
    const { container } = render(
      <LockStatusIndicator lock={mockLock} className="custom-class" />
    );
    expect(container.firstChild).toHaveClass("custom-class");
  });
});
