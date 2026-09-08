/** StatusBadge: what a deployment *is*, and — separately — whether it has
 *  settled.
 *
 * The badge is the only place several lists say what a deployment is doing, so
 * the text has to be right for a status the frontend has never heard of too —
 * a new backend state must read as itself, not as blank.
 *
 * `sync` is the second question. Since deletes became asynchronous the record
 * outlives the request that asked for it to go, and *running · deleting* is a
 * real situation: a badge that collapsed the two into one word would say
 * "stopped" about a container still holding 90 GB of VRAM. */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import StatusBadge, { isSettling } from "@/components/StatusBadge";

/** The dot is the pill's own first child, not the wrapper's. */
const dot = (container: HTMLElement) => container.querySelector("span > span > span");

describe("StatusBadge", () => {
  it.each([
    ["running", "Running"],
    ["stopped", "Stopped"],
    ["error", "Error"],
    ["pending", "Pending"],
    ["pulling", "Pulling"],
  ])("renders %s as %s", (status, label) => {
    render(<StatusBadge status={status} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  /** A status with no colour of its own still has to name itself. */
  it("names a status it has no colour for rather than showing nothing", () => {
    render(<StatusBadge status="quiescing" />);
    expect(screen.getByText("Quiescing")).toBeInTheDocument();
  });

  it("matches a status whatever case it arrives in", () => {
    render(<StatusBadge status="RUNNING" />);
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
  });

  it("animates only the running dot", () => {
    const { container, unmount } = render(<StatusBadge status="running" />);
    expect(dot(container)).toHaveStyle({ animation: "pulse 2s infinite" });
    unmount();

    const stopped = render(<StatusBadge status="stopped" />);
    expect(dot(stopped.container)).toHaveStyle({ animation: "none" });
  });

  describe("convergence", () => {
    it("says nothing extra about a settled deployment", () => {
      const { container } = render(<StatusBadge status="running" sync="in_sync" />);

      expect(screen.getByText("Running")).toBeInTheDocument();
      expect(container.querySelectorAll("[data-testid^='sync-']")).toHaveLength(0);
    });

    it("says nothing extra when the backend sent no sync at all", () => {
      // Every record written before the reconciler existed looks like this.
      const { container } = render(<StatusBadge status="running" />);

      expect(container.querySelectorAll("[data-testid^='sync-']")).toHaveLength(0);
    });

    it("keeps the lifecycle and the convergence apart", () => {
      // A deployment being stopped is still running until a node says
      // otherwise; the badge has to be able to say both at once.
      render(<StatusBadge status="running" sync="in_progress" />);

      expect(screen.getByText("Running")).toBeInTheDocument();
      expect(screen.getByTestId("sync-in_progress")).toHaveTextContent("in progress");
    });

    it("shows a record being cleared", () => {
      render(<StatusBadge status="stopped" sync="deleting" />);

      expect(screen.getByText("Stopped")).toBeInTheDocument();
      expect(screen.getByTestId("sync-deleting")).toHaveTextContent("deleting");
    });

    /** A node that could not be asked has not said no. The chip says the
     *  record is unverified rather than inventing a state from silence. */
    it("reports a node it could not ask as unverified, not as failed", () => {
      render(<StatusBadge status="running" sync="unknown" syncReason="a node could not be asked" />);

      const chip = screen.getByTestId("sync-unknown");
      expect(chip).toHaveTextContent("unverified");
      expect(chip).toHaveAttribute("title", "a node could not be asked");
    });

    it("ignores a sync state from a newer backend rather than rendering a blank chip", () => {
      const { container } = render(<StatusBadge status="running" sync="quantum" />);

      expect(container.querySelectorAll("[data-testid^='sync-']")).toHaveLength(0);
    });
  });

  describe("isSettling", () => {
    it.each([
      ["in_progress", true],
      ["deleting", true],
      ["in_sync", false],
      // Unverified is not mid-change: the operator can still act on it, and
      // refusing to would leave a record nobody could ever remove.
      ["unknown", false],
      [undefined, false],
    ])("%s → %s", (sync, expected) => {
      expect(isSettling(sync as string | undefined)).toBe(expected);
    });
  });
});
