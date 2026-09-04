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
