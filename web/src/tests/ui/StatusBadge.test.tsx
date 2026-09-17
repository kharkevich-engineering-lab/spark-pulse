/** StatusBadge: what a deployment *is*, and — separately — whether it has
 *  settled.
 *
 * The badge is the only place several lists say what a deployment is doing, so
 * the text has to be right for a status the frontend has never heard of too —
 * a new backend state must read as itself, not as blank and not as a
 * translation key.
 *
 * `sync` is the second question. Since deletes became asynchronous the record
 * outlives the request that asked for it to go, and *running · deleting* is a
 * real situation: a badge that collapsed the two into one word would say
 * "stopped" about a container still holding 90 GB of VRAM. */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nProvider } from "@/lib/i18n";
import StatusBadge, { isSettling, statusTone } from "@/ui/StatusBadge";

/** The dot is the first child of the pill, and it is `aria-hidden`. */
const dots = (container: HTMLElement) => container.querySelectorAll("span[aria-hidden]");

describe("StatusBadge", () => {
  it.each([
    ["running", "Running"],
    ["stopped", "Stopped"],
    ["error", "Error"],
    ["pending", "Pending"],
    ["pulling", "Pulling"],
    ["ready", "Ready"],
    ["starting", "Starting"],
    ["unknown", "Unknown"],
  ])("renders %s as %s", (status, label) => {
    render(<StatusBadge status={status} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  /** Through `t()`, so the French page is in French — it used to capitalise
   *  whatever the API sent, which is English whichever language you chose. */
  it("says the status in the reader's own language", () => {
    render(
      <I18nProvider>
        <StatusBadge status="running" />
      </I18nProvider>,
    );

    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  /** A status this build has never heard of has no key to look up, and
   *  `status.quiescing` on screen would be worse than "Quiescing". */
  it("names a status it has no key for rather than showing the key", () => {
    render(<StatusBadge status="quiescing" />);
    expect(screen.getByText("Quiescing")).toBeInTheDocument();
  });

  it("matches a status whatever case it arrives in", () => {
    render(<StatusBadge status="RUNNING" />);
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("colours each status from the one vocabulary", () => {
    expect(statusTone("running")).toBe("good");
    expect(statusTone("pulling")).toBe("warn");
    expect(statusTone("error")).toBe("bad");
    expect(statusTone("stopped")).toBe("muted");
    // Anything unrecognised is muted rather than alarming.
    expect(statusTone("quiescing")).toBe("muted");
  });

  /** Tailwind's own keyframe, not a `pulse` this app never defined: the
   *  inline `animation: pulse 2s infinite` depended on a keyframe nothing in
   *  the stylesheet declared, so the dot never moved. */
  it("animates only the running dot", () => {
    const { container, unmount } = render(<StatusBadge status="running" />);
    expect(dots(container)[0]).toHaveClass("animate-pulse");
    unmount();

    const stopped = render(<StatusBadge status="stopped" />);
    expect(dots(stopped.container)[0]).not.toHaveClass("animate-pulse");
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
      expect(screen.getByTestId("sync-in_progress")).toHaveTextContent("In progress");
    });

    it("shows a record being cleared", () => {
      render(<StatusBadge status="stopped" sync="deleting" />);

      expect(screen.getByText("Stopped")).toBeInTheDocument();
      expect(screen.getByTestId("sync-deleting")).toHaveTextContent("Deleting");
    });

    /** A node that could not be asked has not said no. The chip says the
     *  record is unverified rather than inventing a state from silence. */
    it("reports a node it could not ask as unverified, not as failed", () => {
      render(<StatusBadge status="running" sync="unknown" syncReason="a node could not be asked" />);

      const chip = screen.getByTestId("sync-unknown");
      expect(chip).toHaveTextContent("Unverified");
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
