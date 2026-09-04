import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import EventStreamViewer, { EventTimeline } from "@/components/EventStreamViewer";
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

  /** The filters are why the viewer is usable during an incident: a
   *  multi-node deploy emits every rank's chatter into one list, and the
   *  question is almost always "what did spark-02 say, and what failed". */
  it("narrows the list to one severity", () => {
    render(<EventStreamViewer events={mockEvents} resource="cluster-1" />);

    fireEvent.change(screen.getByDisplayValue("All Severities"), { target: { value: "error" } });

    expect(screen.getByText("Deployment failed")).toBeInTheDocument();
    expect(screen.queryByText("Cluster starting")).not.toBeInTheDocument();
    expect(screen.getByText("1 events")).toBeInTheDocument();
  });

  it("narrows the list to one node, offering only the nodes that spoke", () => {
    render(<EventStreamViewer events={mockEvents} resource="cluster-1" />);

    const nodePicker = screen.getByDisplayValue("All Nodes");
    expect(within(nodePicker).getByRole("option", { name: "10.0.0.1" })).toBeInTheDocument();
    expect(within(nodePicker).getByRole("option", { name: "10.0.0.2" })).toBeInTheDocument();

    fireEvent.change(nodePicker, { target: { value: "10.0.0.2" } });

    expect(screen.getByText("Deployment failed")).toBeInTheDocument();
    expect(screen.queryByText("Ray cluster ready")).not.toBeInTheDocument();
  });

  it("says the filters matched nothing rather than looking broken", () => {
    render(<EventStreamViewer events={mockEvents} resource="cluster-1" />);

    fireEvent.change(screen.getByDisplayValue("All Severities"), { target: { value: "warning" } });

    expect(screen.getByText("No events to display")).toBeInTheDocument();
    expect(screen.getByText("0 events")).toBeInTheDocument();
  });

  it("turns an event type into a readable label", () => {
    render(<EventStreamViewer events={mockEvents} resource="cluster-1" />);
    expect(screen.getByText("Deployment Start")).toBeInTheDocument();
    expect(screen.getByText("Ray Cluster Ready")).toBeInTheDocument();
  });

  it("shows the node, the actor and the correlation id when an event carries them", () => {
    render(
      <EventStreamViewer
        events={[
          {
            ...mockEvents[0],
            actor: "alex",
            correlation_id: "0123456789abcdef",
          },
        ]}
        resource="cluster-1"
      />
    );

    // Scoped to the row: the node also appears as an option in the filter.
    const row = screen.getByText("Cluster starting").closest("div")!.parentElement!;
    expect(within(row).getByText("10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("by alex")).toBeInTheDocument();
    // Truncated: the full id is noise in a timeline, the prefix is enough to
    // tie two events together.
    expect(screen.getByText("01234567")).toBeInTheDocument();
  });

  it("renders an event that names no node or actor", () => {
    render(
      <EventStreamViewer
        events={[{ ...mockEvents[0], node: undefined, actor: undefined }]}
        resource="cluster-1"
      />
    );
    expect(screen.getByText("Cluster starting")).toBeInTheDocument();
    expect(screen.queryByText("10.0.0.1")).not.toBeInTheDocument();
  });

  /** An event type the frontend has not been taught still has to appear: a
   *  backend that gained a new event must not make the viewer crash or hide
   *  it. */
  it("renders an event type it does not recognise", () => {
    render(
      <EventStreamViewer
        events={[{ ...mockEvents[0], event_type: "gpu_fell_over" as EventType }]}
        resource="cluster-1"
      />
    );
    expect(screen.getByText("Gpu Fell Over")).toBeInTheDocument();
  });

  it("offers no Clear button when there is nothing to clear", () => {
    render(<EventStreamViewer events={[]} resource="cluster-1" onClear={vi.fn()} />);
    expect(screen.queryByText("Clear")).not.toBeInTheDocument();
  });

  it("offers no Clear button when the page has nowhere to clear to", () => {
    render(<EventStreamViewer events={mockEvents} resource="cluster-1" />);
    expect(screen.queryByText("Clear")).not.toBeInTheDocument();
  });
});

/** EventTimeline: the milestones of one deploy, with the chatter left out. */
describe("EventTimeline", () => {
  it("renders nothing when no lifecycle milestone has happened yet", () => {
    const { container } = render(
      <EventTimeline
        events={[
          {
            ...mockEvents[0],
            event_id: "only",
            event_type: EventType.HEALTH_CHECK_PASS,
          },
        ]}
      />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("keeps the milestones and drops everything else", () => {
    render(
      <EventTimeline
        events={[
          { ...mockEvents[0], event_id: "a", event_type: EventType.DEPLOYMENT_START },
          { ...mockEvents[1], event_id: "b", event_type: EventType.HEALTH_CHECK_PASS, message: "probe ok" },
          { ...mockEvents[2], event_id: "c", event_type: EventType.DEPLOYMENT_SUCCESS, message: "served" },
        ]}
      />
    );

    expect(screen.getByText("Deployment Timeline")).toBeInTheDocument();
    expect(screen.getByText("Cluster starting")).toBeInTheDocument();
    expect(screen.getByText("served")).toBeInTheDocument();
    expect(screen.queryByText("probe ok")).not.toBeInTheDocument();
  });

  it("takes a className from the page that placed it", () => {
    const { container } = render(
      <EventTimeline
        events={[{ ...mockEvents[0], event_type: EventType.DEPLOYMENT_START }]}
        className="mt-4"
      />
    );
    expect(container.firstChild).toHaveClass("mt-4");
  });
});
