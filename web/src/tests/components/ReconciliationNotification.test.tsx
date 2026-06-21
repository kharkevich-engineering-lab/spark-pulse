import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ReconciliationNotification, {
  type ReconciliationResult,
} from "@/components/ReconciliationNotification";

const mockResult: ReconciliationResult = {
  reconstructed_clusters: [
    {
      name: "cluster-1",
      head_ip: "10.0.0.1",
      worker_ips: ["10.0.0.2", "10.0.0.3"],
      source: "docker_labels",
    },
  ],
  orphaned_containers: [
    {
      container_id: "abc123",
      container_name: "orphaned-container",
      reason: "No matching cluster found",
    },
  ],
  last_reconciliation: new Date().toISOString(),
};

describe("ReconciliationNotification", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns null when result is null", () => {
    const { container } = render(
      <ReconciliationNotification result={null} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("returns null when no issues found", () => {
    const emptyResult = {
      reconstructed_clusters: [],
      orphaned_containers: [],
      last_reconciliation: new Date().toISOString(),
    };
    const { container } = render(
      <ReconciliationNotification result={emptyResult} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("shows reconstructed clusters", () => {
    render(<ReconciliationNotification result={mockResult} />);
    expect(screen.getByText("Reconciliation Complete")).toBeInTheDocument();
    expect(screen.getByText("cluster-1")).toBeInTheDocument();
    expect(screen.getByText("head: 10.0.0.1")).toBeInTheDocument();
  });

  it("shows orphaned containers", () => {
    render(<ReconciliationNotification result={mockResult} />);
    expect(screen.getByText("orphaned-container")).toBeInTheDocument();
    expect(screen.getByText("(No matching cluster found)")).toBeInTheDocument();
  });

  it("calls onDismiss when close button is clicked", () => {
    const onDismiss = vi.fn();
    render(<ReconciliationNotification result={mockResult} onDismiss={onDismiss} />);
    // The close button has no text, so we find it by its SVG icon
    const buttons = screen.getAllByRole("button");
    fireEvent.click(buttons[0]); // Close button (first button)
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("calls onCleanOrphans when clean all button is clicked", async () => {
    const onCleanOrphans = vi.fn().mockResolvedValue(undefined);
    render(
      <ReconciliationNotification
        result={mockResult}
        onCleanOrphans={onCleanOrphans}
      />
    );
    // Check the checkbox first
    const checkbox = screen.getByRole("checkbox");
    fireEvent.click(checkbox);
    // Then click the "Clean All" button
    fireEvent.click(screen.getByText("Clean All"));

    await waitFor(() => {
      expect(onCleanOrphans).toHaveBeenCalledWith(["abc123"]);
    });
  });

  it("applies custom className", () => {
    const { container } = render(
      <ReconciliationNotification result={mockResult} className="custom-class" />
    );
    expect(container.firstChild).toHaveClass("custom-class");
  });
});
