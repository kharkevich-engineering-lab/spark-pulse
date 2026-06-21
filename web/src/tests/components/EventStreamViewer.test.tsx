import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import EventStreamViewer from "@/components/EventStreamViewer";
import { EventType } from "@/lib/operations";

const mockEvents = [
  {
    event_id: "1",
    event_type: EventType.DEPLOYMENT_START,
    timestamp: new Date("2026-01-01T00:00:00Z").toISOString(),
    resource: "cluster-1",
    resource_type: "cluster" as const,
    message: "Cluster starting",
    metadata: {},
    severity: "info" as const,
    node: "10.0.0.1",
  },
  {
    event_id: "2",
    event_type: EventType.RAY_CLUSTER_READY,
    timestamp: new Date("2026-01-01T00:01:00Z").toISOString(),
    resource: "cluster-1",
    resource_type: "cluster" as const,
    message: "Ray cluster ready",
    metadata: {},
    severity: "info" as const,
    node: "10.0.0.1",
  },
  {
    event_id: "3",
    event_type: EventType.DEPLOYMENT_FAILURE,
    timestamp: new Date("2026-01-01T00:02:00Z").toISOString(),
    resource: "cluster-1",
    resource_type: "cluster" as const,
    message: "Deployment failed",
    metadata: {},
    severity: "error" as const,
    node: "10.0.0.2",
  },
];

describe("EventStreamViewer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the component with header", () => {
    render(<EventStreamViewer events={mockEvents} resource="cluster-1" />);
    expect(screen.getByText("Event Stream")).toBeInTheDocument();
  });

  it("renders all events", () => {
    render(<EventStreamViewer events={mockEvents} resource="cluster-1" />);
    expect(screen.getByText("Cluster starting")).toBeInTheDocument();
    expect(screen.getByText("Ray cluster ready")).toBeInTheDocument();
    expect(screen.getByText("Deployment failed")).toBeInTheDocument();
  });

  it("shows event count", () => {
    render(<EventStreamViewer events={mockEvents} resource="cluster-1" />);
    expect(screen.getByText("3 events")).toBeInTheDocument();
  });

  it("shows empty state when no events", () => {
    render(
      <EventStreamViewer
        events={[]}
        resource="cluster-1"
      />
    );
    expect(screen.getByText("No events to display")).toBeInTheDocument();
  });

  it("clears events when clear button is clicked", () => {
    const onClear = vi.fn();
    render(
      <EventStreamViewer
        events={mockEvents}
        resource="cluster-1"
        onClear={onClear}
      />
    );
    fireEvent.click(screen.getByText("Clear"));
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it("applies custom className", () => {
    const { container } = render(
      <EventStreamViewer events={mockEvents} resource="cluster-1" className="custom-class" />
    );
    expect(container.firstChild).toHaveClass("custom-class");
  });
});
