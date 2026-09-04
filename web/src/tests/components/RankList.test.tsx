/** RankList: a solo deployment stays quiet, a gang lists every rank, and an
 * orphan is surfaced with what it blocks. */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import RankList from "@/components/RankList";
import type { DeploymentOrphan, DeploymentRank } from "@/lib/types";

const rank = (over: Partial<DeploymentRank> = {}): DeploymentRank => ({
  rank: 0,
  node: "",
  host: "",
  container_name: "spark-pulse-dep-r0-g1",
  is_head: true,
  ...over,
});

const orphan = (over: Partial<DeploymentOrphan> = {}): DeploymentOrphan => ({
  rank: 1,
  node: "spark-02",
  container_name: "spark-pulse-dep-r1-g1",
  reason: "the node could not be reached: timed out",
  since: "2026-01-01T00:00:00+00:00",
  ...over,
});

describe("RankList", () => {
  it("stays quiet for a solo, healthy, control-node deployment", () => {
    const { container } = render(<RankList ranks={[rank()]} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("rank-list")).not.toBeInTheDocument();
  });

  it("renders without crashing for a record with no ranks field at all", () => {
    const { container } = render(<RankList />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists every rank of a three-rank deployment and marks the head", () => {
    render(
      <RankList
        ranks={[
          rank({ rank: 0, node: "", is_head: true, container_name: "dep-r0" }),
          rank({ rank: 1, node: "spark-02", is_head: false, container_name: "dep-r1" }),
          rank({ rank: 2, node: "spark-03", is_head: false, container_name: "dep-r2" }),
        ]}
      />,
    );
    expect(screen.getByTestId("rank-row-0")).toBeInTheDocument();
    expect(screen.getByTestId("rank-row-1")).toBeInTheDocument();
    expect(screen.getByTestId("rank-row-2")).toBeInTheDocument();
    expect(screen.getByTestId("rank-row-0")).toHaveTextContent(/head/i);
    expect(screen.getByTestId("rank-row-1")).not.toHaveTextContent(/head/i);
    expect(screen.getByTestId("rank-row-1")).toHaveTextContent("spark-02");
    expect(screen.getByTestId("rank-row-2")).toHaveTextContent("spark-03");
  });

  it("renders an empty node as the control node, not a blank cell", () => {
    render(<RankList ranks={[rank({ node: "" }), rank({ rank: 1, node: "spark-02", is_head: false })]} />);
    expect(screen.getByTestId("rank-row-0")).toHaveTextContent("this node");
  });

  it("says so at any size when a solo rank is unhealthy", () => {
    render(
      <RankList
        ranks={[
          rank({
            container: { status: "exited", running: false, id: "abc", state: {}, error: null },
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("rank-list")).toBeInTheDocument();
    expect(screen.getByTestId("rank-row-0")).toHaveTextContent("exited");
  });

  it("surfaces an orphan with its node, its container, and what it blocks", () => {
    render(<RankList ranks={[rank()]} orphans={[orphan()]} />);
    const row = screen.getByTestId("rank-orphan");
    expect(row).toHaveTextContent("spark-02");
    expect(row).toHaveTextContent("spark-pulse-dep-r1-g1");
    expect(row).toHaveTextContent(/could not be reached/);
    expect(row).toHaveTextContent(/holding spark-02's ports/);
  });

  it("names the control node when an orphan's node is empty", () => {
    render(<RankList orphans={[orphan({ node: "" })]} />);
    const row = screen.getByTestId("rank-orphan");
    expect(row).toHaveTextContent("this node");
    expect(row).toHaveTextContent(/holding this node's ports/);
  });
});
