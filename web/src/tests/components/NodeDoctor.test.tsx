import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NodeDoctor from "@/components/NodeDoctor";
import type { ClusterNode, DoctorReport } from "@/lib/types";

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

  it("diagnoses on open and groups findings by who can fix them", async () => {
    render(<NodeDoctor node={node()} onClose={() => {}} onChanged={() => {}} />);
    const findings = await screen.findByTestId("doctor-findings");
    expect(within(findings).getByText("docker-socket")).toBeInTheDocument();
    expect(findings).toHaveTextContent("Fixable from here");
    expect(findings).toHaveTextContent("Needs someone on that machine");
    expect(findings).toHaveTextContent("usermod -aG docker spark");
    expect(screen.getByText("2 to look at")).toBeInTheDocument();
    expect(fetchNodeDoctor).toHaveBeenCalledWith("peer");
    // Diagnosis alone never repairs.
    expect(treatNode).not.toHaveBeenCalled();
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
    render(<NodeDoctor node={node()} onClose={() => {}} onChanged={() => {}} />);
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
    render(<NodeDoctor node={node()} onClose={() => {}} onChanged={() => {}} />);
    await screen.findByTestId("doctor-findings");
    expect(screen.queryByRole("button", { name: "Repair what is fixable" })).toBeNull();
  });

  it("does not offer a repair on the control node", async () => {
    render(<NodeDoctor node={node({ is_control_plane: true })} onClose={() => {}} onChanged={() => {}} />);
    await screen.findByTestId("doctor-findings");
    expect(screen.queryByRole("button", { name: "Repair what is fixable" })).toBeNull();
    expect(screen.queryByLabelText("sudo password")).toBeNull();
  });

  it("surfaces a failure to diagnose", async () => {
    vi.mocked(fetchNodeDoctor).mockRejectedValue(new Error("API 503: the agent transport is not running"));
    render(<NodeDoctor node={node()} onClose={() => {}} onChanged={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("transport is not running");
  });

  it("keeps the report and says why when a repair fails", async () => {
    const user = userEvent.setup();
    vi.mocked(treatNode).mockRejectedValue(new Error("API 502: sudo password refused"));
    render(<NodeDoctor node={node()} onClose={() => {}} onChanged={() => {}} />);
    await screen.findByTestId("doctor-findings");
    await user.click(screen.getByRole("button", { name: "Repair what is fixable" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("sudo password refused");
    expect(screen.getByTestId("doctor-findings")).toBeInTheDocument();
  });
});
