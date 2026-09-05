/** StatusBadge: a deployment's state, spelled out rather than colour-coded.
 *
 * The badge is the only place several lists say what a deployment is doing, so
 * the text has to be right for a status the frontend has never heard of too —
 * a new backend state must read as itself, not as blank. */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import StatusBadge from "@/components/StatusBadge";

describe("StatusBadge", () => {
  it.each([
    ["running", "Running"],
    ["stopped", "Stopped"],
    ["error", "Error"],
    ["pending", "Pending"],
  ])("renders %s as %s", (status, label) => {
    render(<StatusBadge status={status} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  /** `starting` and `pulling` are real backend statuses with no colour of
   *  their own. They still have to name themselves. */
  it("names a status it has no colour for rather than showing nothing", () => {
    render(<StatusBadge status="pulling" />);
    expect(screen.getByText("Pulling")).toBeInTheDocument();
  });

  it("matches a status whatever case it arrives in", () => {
    render(<StatusBadge status="RUNNING" />);
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
  });

  it("animates only the running dot", () => {
    const { container, unmount } = render(<StatusBadge status="running" />);
    expect(container.querySelector("span span")).toHaveStyle({ animation: "pulse 2s infinite" });
    unmount();

    const stopped = render(<StatusBadge status="stopped" />);
    expect(stopped.container.querySelector("span span")).toHaveStyle({ animation: "none" });
  });
});
