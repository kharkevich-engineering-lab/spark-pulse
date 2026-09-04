/** The pre-flight panel: three verdicts shown as three verdicts, and every
 * row naming its node and its remedy. */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import PreflightPanel, { checksToShow, describeCost } from "@/components/PreflightPanel";
import type { PreflightCheck, PreflightReport } from "@/lib/types";

const check = (over: Partial<PreflightCheck> = {}): PreflightCheck => ({
  id: "image",
  title: "Engine image",
  node: "spark-02",
  node_id: "peer-1",
  status: "warn",
  observed: "the image is not on spark-02; about 25.0 GB will transfer",
  remedy: "Pre-seed it with POST /api/images/sync.",
  delay_bytes: 26_843_545_600,
  costs_time: true,
  detail: {},
  ...over,
});

const report = (over: Partial<PreflightReport> = {}): PreflightReport => {
  const base: PreflightReport = {
    verdict: "ready",
    summary: "ready: every check passed",
    can_proceed: true,
    delays: false,
    estimated_transfer_bytes: 0,
    counts: { pass: 9, warn: 0, fail: 0 },
    nodes: [
      { id: "c", label: "spark-01", address: "", is_control_plane: true, ranks: [0] },
    ],
    checks: [],
    blocking: [],
    delaying: [],
    advisories: [],
    plan: {
      recipe_id: "r",
      engine: "vllm",
      variant: "default",
      image_ref: "ghcr.io/example/vllm:0.1.0",
      model: "Qwen/Qwen3-8B",
      port: 9000,
      rendezvous_port: null,
      node_count: 1,
    },
    checked_at: "2026-01-01T00:00:00+00:00",
  };
  return { ...base, ...over };
};

describe("checksToShow", () => {
  it("puts what blocks before what merely delays", () => {
    const failed = check({ id: "reachability", status: "fail" });
    const slow = check();
    const advisory = check({ id: "gpu", costs_time: false, delay_bytes: 0 });
    const rows = checksToShow(
      report({ blocking: [failed], delaying: [slow], advisories: [advisory] }),
    );
    expect(rows.map((c) => c.id)).toEqual(["reachability", "image", "gpu"]);
  });

  it("lists nothing when everything passed", () => {
    expect(checksToShow(report())).toEqual([]);
  });
});

describe("describeCost", () => {
  it("says what a slow deployment will move, and where", () => {
    expect(
      describeCost(
        report({
          verdict: "slow",
          delaying: [check()],
          estimated_transfer_bytes: 26_843_545_600,
        }),
      ),
    ).toBe("25.0 GB has to transfer to spark-02 before this starts");
  });

  it("does not invent a size it was not given", () => {
    expect(
      describeCost(
        report({
          verdict: "slow",
          delaying: [check({ delay_bytes: 0 })],
          estimated_transfer_bytes: 0,
        }),
      ),
    ).toContain("data of unreported size");
  });

  it("counts the failures and names their nodes when blocked", () => {
    expect(
      describeCost(
        report({
          verdict: "blocked",
          can_proceed: false,
          blocking: [check({ status: "fail", node: "spark-03" })],
        }),
      ),
    ).toBe("1 check failed on spark-03");
  });
});

describe("PreflightPanel", () => {
  it("shows a blocked verdict as a stop, not as a slow deployment", () => {
    render(
      <PreflightPanel
        report={report({
          verdict: "blocked",
          can_proceed: false,
          counts: { pass: 2, warn: 0, fail: 1 },
          blocking: [
            check({
              id: "reachability",
              title: "Reachable",
              status: "fail",
              observed: "could not open a connection to spark-02 (10.0.0.11)",
              remedy: "Check that spark-02 is powered on and routable.",
            }),
          ],
        })}
      />,
    );
    expect(screen.getByTestId("preflight-verdict")).toHaveTextContent("Blocked");
    expect(screen.getByText(/could not open a connection to spark-02/)).toBeInTheDocument();
    expect(screen.getByText(/powered on and routable/)).toBeInTheDocument();
    expect(screen.getByTestId("preflight-check-fail")).toBeInTheDocument();
  });

  it("shows a slow deployment as ready with a wait attached", () => {
    render(
      <PreflightPanel
        report={report({
          verdict: "slow",
          delaying: [check()],
          estimated_transfer_bytes: 26_843_545_600,
        })}
      />,
    );
    expect(screen.getByTestId("preflight-verdict")).toHaveTextContent("Ready, but slow");
    expect(screen.getByTestId("preflight-check-warn")).toBeInTheDocument();
    expect(screen.queryByTestId("preflight-check-fail")).not.toBeInTheDocument();
  });

  it("counts the passing checks rather than listing nine green rows", () => {
    render(<PreflightPanel report={report()} />);
    expect(screen.getByTestId("preflight-verdict")).toHaveTextContent("Ready");
    expect(screen.getByText(/9 checks passed across 1 node/)).toBeInTheDocument();
    expect(screen.queryByTestId("preflight-checks")).not.toBeInTheDocument();
    expect(screen.getByText(/Nothing to fix/)).toBeInTheDocument();
  });

  it("names the node on every row it shows", () => {
    render(
      <PreflightPanel
        report={report({
          verdict: "blocked",
          blocking: [check({ status: "fail", node: "spark-02" })],
          advisories: [check({ id: "gpu", node: "spark-01", costs_time: false })],
        })}
      />,
    );
    const rows = screen.getByTestId("preflight-checks");
    expect(rows).toHaveTextContent("spark-02");
    expect(rows).toHaveTextContent("spark-01");
  });
});
