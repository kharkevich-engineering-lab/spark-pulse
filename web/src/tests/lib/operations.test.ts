/** The two pure functions the seeded event panel rests on.
 *
 * `eventFromFrame` is the one parser for both sources of a run's timeline —
 * the SSE stream and the stored history — and `mergeEvents` is what keeps an
 * event that is in both from being shown twice.
 */

import { describe, it, expect } from "vitest";
import {
  eventFromFrame,
  mergeEvents,
  type DeploymentEvent,
  type DeploymentEventFrame,
} from "@/lib/operations";

function event(over: Partial<DeploymentEvent> = {}): DeploymentEvent {
  return eventFromFrame({
    event_id: "e1",
    timestamp: "2026-01-01T00:00:00Z",
    type: "deployment_planned",
    message: "planned",
    resource: "d1",
    resource_type: "deployment",
    ...(over as DeploymentEventFrame),
  });
}

describe("eventFromFrame", () => {
  it("keeps the id the backend minted", () => {
    expect(eventFromFrame({ event_id: "abc", type: "deployment_ready" }).event_id).toBe(
      "abc",
    );
  });

  it("renders a frame that arrived without an id or a time", () => {
    // Still evidence that something happened; what it cannot be is
    // deduplicated, because the id it gets is unique to this browser.
    const parsed = eventFromFrame({ type: "deployment_error", resource: "d1" });

    expect(parsed.event_id).toHaveLength(36);
    expect(Number.isNaN(new Date(parsed.timestamp).getTime())).toBe(false);
    expect(parsed.resource_type).toBe("deployment");
  });

  it("carries the node and the severity the store recorded", () => {
    const parsed = eventFromFrame({
      type: "deployment_error",
      node: "node-b",
      severity: "error",
    });

    expect(parsed.node).toBe("node-b");
    expect(parsed.severity).toBe("error");
  });

  it("leaves an absent node and severity absent rather than empty", () => {
    const parsed = eventFromFrame({ type: "deployment_ready", node: "", severity: "" });

    expect(parsed.node).toBeUndefined();
    expect(parsed.severity).toBeUndefined();
  });
});

describe("mergeEvents", () => {
  it("shows an event held by both sources once", () => {
    const live = event({ message: "from the stream" } as Partial<DeploymentEvent>);
    const stored = event({ message: "from the store" } as Partial<DeploymentEvent>);

    const merged = mergeEvents([live], [stored]);

    expect(merged).toHaveLength(1);
    expect(merged[0].message).toBe("from the stream");
  });

  it("puts the newest first, whichever list it came from", () => {
    const older = eventFromFrame({
      event_id: "a",
      timestamp: "2026-01-01T00:00:00Z",
      type: "deployment_planned",
      message: "planned",
    });
    const newer = eventFromFrame({
      event_id: "b",
      timestamp: "2026-01-01T00:05:00Z",
      type: "deployment_ready",
      message: "serving",
    });

    expect(mergeEvents([older], [newer]).map((e) => e.message)).toEqual([
      "serving",
      "planned",
    ]);
  });

  it("is empty for no sources at all", () => {
    expect(mergeEvents()).toEqual([]);
    expect(mergeEvents([], [])).toEqual([]);
  });
});
