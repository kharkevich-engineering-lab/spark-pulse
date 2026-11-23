import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NodeDoctor from "@/components/NodeDoctor";
import type { ClusterNode, DoctorFinding, DoctorReport, NodeTuningId } from "@/lib/types";

vi.mock("@/lib/api", () => ({ fetchNodeDoctor: vi.fn(), treatNode: vi.fn() }));

import { fetchNodeDoctor, treatNode } from "@/lib/api";

function node(over: Partial<ClusterNode> = {}): ClusterNode {
  return {
    id: "peer",
    name: "gx10-b90f",
    address: "10.0.0.11",
    is_control_plane: false,
    ssh_user: "spark",
    ssh_key_path: "",
    ethernet_interface: "",
    infiniband_interfaces: [],
    state: "healthy",
    last_seen: null,
    machine_id: "",
    ...over,
  };
}

function report(over: Partial<DoctorReport> = {}): DoctorReport {
  return {
    node_id: "peer",
    host: "10.0.0.11",
    channels: ["ssh", "agent"],
    findings: [
      { check: "unit", status: "ok", detail: "active and enabled", channel: "ssh", verdict: "nothing-to-do", remedy: "" },
      {
        check: "docker-socket",
        status: "broken",
        detail: "spark is not in the docker group",
        channel: "ssh",
        verdict: "fixable-here",
        remedy: "usermod -aG docker spark, then restart the manager",
      },
      {
        check: "disk",
        status: "warn",
        detail: "4 GiB free",
        channel: "ssh",
        verdict: "needs-a-human-on-that-machine",
        remedy: "free space on that machine",
      },
    ],
    repairs: [],
    healthy: false,
    ...over,
  };
}

describe("NodeDoctor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchNodeDoctor).mockResolvedValue(report());
    vi.mocked(treatNode).mockResolvedValue(report());
  });

  it("diagnoses on open, worst first, and says who can fix each", async () => {
    const user = userEvent.setup();
    render(<NodeDoctor node={node()} onChanged={() => {}} />);
    const findings = await screen.findByTestId("doctor-findings");
    const rows = within(findings).getAllByRole("row");
    // Header, the broken one, the warning, then the fold for the rest.
    expect(rows[1]).toHaveTextContent("docker-socket");
    expect(rows[1]).toHaveTextContent("Fixable from here");
    expect(rows[2]).toHaveTextContent("disk");
    expect(rows[2]).toHaveTextContent("Needs someone on that node");
    expect(screen.getByText("2 to look at")).toBeInTheDocument();
    // The remedy is one hover away, not a paragraph under every check.
    expect(findings).not.toHaveTextContent("usermod -aG docker spark");
    await user.click(within(rows[1]).getByRole("button", { name: "How to fix" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("usermod -aG docker spark");
    expect(fetchNodeDoctor).toHaveBeenCalledWith("peer");
    // Diagnosis alone never repairs.
    expect(treatNode).not.toHaveBeenCalled();
  });

  it("folds what passed into one row, and opens it on request", async () => {
    const user = userEvent.setup();
    render(<NodeDoctor node={node()} onChanged={() => {}} />);
    const findings = await screen.findByTestId("doctor-findings");
    expect(within(findings).queryByText("unit")).toBeNull();
    const fold = within(findings).getByRole("button", { name: "1 more check, passed or not looked at" });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    await user.click(fold);
    expect(within(findings).getByTestId("doctor-check-unit")).toHaveTextContent("active and enabled");
    // A passing check carries no remedy and no verdict.
    expect(within(screen.getByTestId("doctor-check-unit")).queryByRole("button")).toBeNull();
  });

  it("offers a repair only for fixable findings, and passes the sudo password", async () => {
    const user = userEvent.setup();
    vi.mocked(treatNode).mockResolvedValue(
      report({
        findings: [{ check: "docker-socket", status: "ok", detail: "reachable", channel: "ssh", verdict: "nothing-to-do", remedy: "" }],
        repairs: [{ check: "docker-socket", action: "usermod -aG docker spark", applied: true, detail: "added and applied" }],
        healthy: true,
      }),
    );
    render(<NodeDoctor node={node()} onChanged={() => {}} />);
    await screen.findByTestId("doctor-findings");
    await user.type(screen.getByLabelText("sudo password"), "s3cret");
    await user.click(screen.getByRole("button", { name: "Repair what is fixable" }));
    await waitFor(() => expect(treatNode).toHaveBeenCalledWith("peer", "s3cret"));
    expect(await screen.findByTestId("doctor-repairs")).toHaveTextContent("added and applied");
    expect(screen.getByText("Nothing wrong that the doctor can see.")).toBeInTheDocument();
  });

  it("shows no repair button when nothing is fixable here", async () => {
    vi.mocked(fetchNodeDoctor).mockResolvedValue(
      report({
        findings: [{ check: "disk", status: "warn", detail: "low", channel: "ssh", verdict: "needs-a-human-on-that-machine", remedy: "free space" }],
      }),
    );
    render(<NodeDoctor node={node()} onChanged={() => {}} />);
    await screen.findByTestId("doctor-findings");
    expect(screen.queryByRole("button", { name: "Repair what is fixable" })).toBeNull();
  });

  it("does not offer a repair on the control node", async () => {
    render(<NodeDoctor node={node({ is_control_plane: true })} onChanged={() => {}} />);
    await screen.findByTestId("doctor-findings");
    expect(screen.queryByRole("button", { name: "Repair what is fixable" })).toBeNull();
    expect(screen.queryByLabelText("sudo password")).toBeNull();
  });

  it("surfaces a failure to diagnose", async () => {
    vi.mocked(fetchNodeDoctor).mockRejectedValue(new Error("API 503: the agent transport is not running"));
    render(<NodeDoctor node={node()} onChanged={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("transport is not running");
  });

  it("keeps the report and says why when a repair fails", async () => {
    const user = userEvent.setup();
    vi.mocked(treatNode).mockRejectedValue(new Error("API 502: sudo password refused"));
    render(<NodeDoctor node={node()} onChanged={() => {}} />);
    await screen.findByTestId("doctor-findings");
    await user.click(screen.getByRole("button", { name: "Repair what is fixable" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("sudo password refused");
    expect(screen.getByTestId("doctor-findings")).toBeInTheDocument();
  });
  describe("node clean-up", () => {
    function tuning(id: NodeTuningId, over: Partial<NonNullable<DoctorFinding["tuning"]>> = {}, status: DoctorFinding["status"] = "warn"): DoctorFinding {
      return {
        check: `tuning-${id.replace("_", "-")}`,
        status,
        detail: "",
        channel: "ssh",
        verdict: status === "warn" ? "fixable-here" : "nothing-to-do",
        remedy: "",
        tuning: {
          id,
          present: true,
          applied: false,
          consequential: false,
          can_treat: true,
          memory_bytes: null,
          apply_commands: [`sudo systemctl disable --now ${id}.service`],
          restore_commands: [`sudo systemctl enable --now ${id}.service`],
          ...over,
        },
      };
    }

    function withTunings(findings: DoctorFinding[]) {
      const faults = report().findings;
      return report({ findings: [...faults, ...findings] });
    }

    it("shows nothing when no clean-up was selected", async () => {
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      await screen.findByTestId("doctor-findings");
      expect(screen.queryByTestId("doctor-tuning")).toBeNull();
    });

    it("says how many of the present ones are applied, and offers Apply or Restore", async () => {
      vi.mocked(fetchNodeDoctor).mockResolvedValue(
        withTunings([
          tuning("bluetooth", { applied: true }, "ok"),
          tuning("printing", { memory_bytes: 15 * 1024 * 1024 }),
          tuning("dgx_dashboard", { present: false }, "ok"),
        ]),
      );
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      const section = await screen.findByTestId("doctor-tuning");

      expect(screen.getByTestId("doctor-tuning-summary")).toHaveTextContent("1 of 2 applied");
      const bluetooth = within(section).getByTestId("doctor-tuning-bluetooth");
      expect(bluetooth).toHaveTextContent("Applied");
      expect(within(bluetooth).getByRole("button", { name: "Restore" })).toBeInTheDocument();
      const printing = within(section).getByTestId("doctor-tuning-printing");
      expect(printing).toHaveTextContent("Not applied");
      expect(printing).toHaveTextContent("Holds 15");
      expect(within(printing).getByRole("button", { name: "Apply" })).toBeInTheDocument();
      const dashboard = within(section).getByTestId("doctor-tuning-dgx_dashboard");
      expect(dashboard).toHaveTextContent("Not on this node");
      expect(within(dashboard).queryByRole("button")).toBeNull();
      expect(treatNode).not.toHaveBeenCalled();
    });

    it("is not a fault: it neither counts as a problem nor offers the repair button", async () => {
      vi.mocked(fetchNodeDoctor).mockResolvedValue(
        report({ findings: [tuning("bluetooth")], healthy: true }),
      );
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      await screen.findByTestId("doctor-tuning");

      expect(screen.getByText("Nothing wrong that the doctor can see.")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Repair what is fixable" })).toBeNull();
      // But a clean-up can be applied, which needs the sudo password field.
      expect(screen.getByLabelText("sudo password")).toBeInTheDocument();
    });

    it("applies one named item with the password, after no confirm for a plain one", async () => {
      const user = userEvent.setup();
      const changed = vi.fn();
      vi.mocked(fetchNodeDoctor).mockResolvedValue(withTunings([tuning("bluetooth")]));
      vi.mocked(treatNode).mockResolvedValue(withTunings([tuning("bluetooth", { applied: true }, "ok")]));
      render(<NodeDoctor node={node()} onChanged={changed} />);
      await screen.findByTestId("doctor-tuning");

      await user.type(screen.getByLabelText("sudo password"), "pw");
      await user.click(within(screen.getByTestId("doctor-tuning-bluetooth")).getByRole("button", { name: "Apply" }));

      await waitFor(() => expect(treatNode).toHaveBeenCalledWith("peer", "pw", { apply: ["bluetooth"] }));
      expect(await screen.findByTestId("doctor-tuning-summary")).toHaveTextContent("1 of 1 applied");
      expect(changed).toHaveBeenCalled();
    });

    it("restores by naming the item", async () => {
      const user = userEvent.setup();
      vi.mocked(fetchNodeDoctor).mockResolvedValue(withTunings([tuning("printing", { applied: true }, "ok")]));
      vi.mocked(treatNode).mockResolvedValue(withTunings([tuning("printing")]));
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      await screen.findByTestId("doctor-tuning");

      await user.click(screen.getByRole("button", { name: "Restore" }));

      await waitFor(() => expect(treatNode).toHaveBeenCalledWith("peer", undefined, { restore: ["printing"] }));
    });

    it("names the text console before the desktop is turned off", async () => {
      const user = userEvent.setup();
      vi.mocked(fetchNodeDoctor).mockResolvedValue(withTunings([tuning("desktop", { consequential: true })]));
      vi.mocked(treatNode).mockResolvedValue(withTunings([tuning("desktop", { consequential: true, applied: true }, "ok")]));
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      await screen.findByTestId("doctor-tuning");

      await user.click(within(screen.getByTestId("doctor-tuning-desktop")).getByRole("button", { name: "Apply" }));

      const dialog = await screen.findByRole("dialog");
      expect(dialog).toHaveTextContent("Apply Desktop session?");
      expect(dialog).toHaveTextContent("text console");
      expect(treatNode).not.toHaveBeenCalled();

      await user.click(within(dialog).getByRole("button", { name: "Apply" }));
      await waitFor(() => expect(treatNode).toHaveBeenCalledWith("peer", undefined, { apply: ["desktop"] }));
    });

    it("cancelling the confirm changes nothing", async () => {
      const user = userEvent.setup();
      vi.mocked(fetchNodeDoctor).mockResolvedValue(withTunings([tuning("desktop", { consequential: true })]));
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      await screen.findByTestId("doctor-tuning");

      await user.click(within(screen.getByTestId("doctor-tuning-desktop")).getByRole("button", { name: "Apply" }));
      await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }));

      expect(treatNode).not.toHaveBeenCalled();
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("lists the snaps that would stop", async () => {
      const user = userEvent.setup();
      vi.mocked(fetchNodeDoctor).mockResolvedValue(
        withTunings([tuning("snapd", { consequential: true, snaps: ["firefox", "gnome-42-2204"] })]),
      );
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      await screen.findByTestId("doctor-tuning");

      await user.click(within(screen.getByTestId("doctor-tuning-snapd")).getByRole("button", { name: "Apply" }));

      expect(await screen.findByRole("dialog")).toHaveTextContent("firefox, gnome-42-2204");
    });

    it("says so when no snap is installed", async () => {
      const user = userEvent.setup();
      vi.mocked(fetchNodeDoctor).mockResolvedValue(withTunings([tuning("snapd", { consequential: true, snaps: [] })]));
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      await screen.findByTestId("doctor-tuning");

      await user.click(within(screen.getByTestId("doctor-tuning-snapd")).getByRole("button", { name: "Apply" }));

      expect(await screen.findByRole("dialog")).toHaveTextContent("No snaps are installed");
    });

    it("gives the control node the commands to run there instead of buttons", async () => {
      const user = userEvent.setup();
      vi.mocked(fetchNodeDoctor).mockResolvedValue(
        report({
          findings: [
            tuning("bluetooth", { can_treat: false }),
            tuning("printing", { can_treat: false, applied: true }, "ok"),
          ],
        }),
      );
      render(<NodeDoctor node={node({ is_control_plane: true })} onChanged={() => {}} />);
      const section = await screen.findByTestId("doctor-tuning");

      expect(within(section).queryByRole("button", { name: "Apply" })).toBeNull();
      expect(within(section).queryByRole("button", { name: "Restore" })).toBeNull();
      expect(screen.queryByLabelText("sudo password")).toBeNull();
      await user.click(within(screen.getByTestId("doctor-tuning-bluetooth")).getByRole("button", { name: "Run on that node" }));
      expect(screen.getByRole("tooltip")).toHaveTextContent("sudo systemctl disable --now bluetooth.service");
    });

    it("shows what could not be read as not read", async () => {
      vi.mocked(fetchNodeDoctor).mockResolvedValue(
        withTunings([{ ...tuning("bluetooth"), status: "unknown", verdict: "nothing-to-do" }]),
      );
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      const row = await screen.findByTestId("doctor-tuning-bluetooth");
      expect(row).toHaveTextContent("Not read");
      expect(within(row).queryByRole("button")).toBeNull();
    });

    it("surfaces a failed change and keeps the report", async () => {
      const user = userEvent.setup();
      vi.mocked(fetchNodeDoctor).mockResolvedValue(withTunings([tuning("bluetooth")]));
      vi.mocked(treatNode).mockRejectedValue(new Error("API 502: sudo password refused"));
      render(<NodeDoctor node={node()} onChanged={() => {}} />);
      await screen.findByTestId("doctor-tuning");

      await user.click(screen.getByRole("button", { name: "Apply" }));

      expect(await screen.findByRole("alert")).toHaveTextContent("sudo password refused");
      expect(screen.getByTestId("doctor-tuning")).toBeInTheDocument();
    });
  });
});
