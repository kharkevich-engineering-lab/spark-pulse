import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ExperimentalBadge, ExperimentalBanner } from "@/components/Experimental";

describe("ExperimentalBadge", () => {
  it("carries a tooltip saying why the feature is marked", () => {
    render(<ExperimentalBadge title="multi-node has not been run on hardware" />);
    expect(screen.getByTitle("multi-node has not been run on hardware")).toBeInTheDocument();
  });

  it("falls back to a general explanation", () => {
    render(<ExperimentalBadge />);
    // A bare "exp" chip with no explanation leaves the operator guessing.
    expect(screen.getByTitle(/not yet verified on real hardware/i)).toBeInTheDocument();
  });
});

describe("ExperimentalBanner", () => {
  it("states plainly what has not been exercised", () => {
    render(<ExperimentalBanner reason="Multi-node bring-up has never been run on real hardware." />);
    expect(screen.getByRole("note")).toHaveTextContent(
      "Multi-node bring-up has never been run on real hardware.",
    );
  });
});

describe("ExperimentalBanner items", () => {
  it("renders each unproven thing as its own line", () => {
    render(
      <ExperimentalBanner
        reason="None of the following has been observed:"
        items={["The rendezvous forming.", "NCCL picking the fabric."]}
      />,
    );
    const note = screen.getByRole("note");
    expect(note.querySelectorAll("li")).toHaveLength(2);
    expect(note).toHaveTextContent("NCCL picking the fabric.");
  });

  it("renders no list when there is nothing left unproven", () => {
    render(<ExperimentalBanner reason="Still experimental." items={[]} />);
    expect(screen.getByRole("note").querySelector("ul")).toBeNull();
  });
});

describe("the multi-node copy", () => {
  it("names risks rather than repeating the word experimental", async () => {
    const { MULTI_NODE_UNPROVEN, MULTI_NODE_REASON } = await import("@/lib/experimental");
    expect(MULTI_NODE_UNPROVEN.length).toBeGreaterThan(0);
    expect(MULTI_NODE_REASON).toMatch(/only one DGX Spark exists/i);
    expect(MULTI_NODE_REASON).toMatch(/never run on two machines|has ever run on two machines/i);
    for (const item of MULTI_NODE_UNPROVEN) {
      expect(item.toLowerCase()).not.toBe("experimental");
      expect(item.length).toBeGreaterThan(30);
    }
  });

  it("claims nothing about hardware verification", async () => {
    const module = await import("@/lib/experimental");
    const prose = [module.MULTI_NODE_REASON, module.MULTI_NODE_TITLE, ...module.MULTI_NODE_UNPROVEN]
      .join(" ")
      .toLowerCase();
    expect(prose).not.toMatch(/verified on|tested on gb10|two machines ran/);
  });
});
