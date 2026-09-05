/** CollectionCard: one OCI recipe collection as it appears in the registry
 * browser. The version and the recipe count are what an operator compares
 * before installing, so both have to be on the card rather than one click in. */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import CollectionCard from "@/components/CollectionCard";
import type { OciCollection } from "@/lib/types";

const collection = (over: Partial<OciCollection> = {}): OciCollection => ({
  name: "spark-recipes",
  version: "1.4.0",
  display_version: "v1.4.0",
  description: "Recipes verified on DGX Spark.",
  vendor: "kharkevich",
  license: "Apache-2.0",
  recipe_count: 12,
  digest: "sha256:abc",
  registry: "ghcr.io",
  ...over,
});

describe("CollectionCard", () => {
  it("shows the name, version, description and what is inside", () => {
    render(
      <CollectionCard
        collection={collection()}
        installed={false}
        onView={vi.fn()}
        onInstall={vi.fn()}
      />,
    );

    expect(screen.getByText("spark-recipes")).toBeInTheDocument();
    expect(screen.getByText("v1.4.0")).toBeInTheDocument();
    expect(screen.getByText("Recipes verified on DGX Spark.")).toBeInTheDocument();
    expect(screen.getByText("12 recipes")).toBeInTheDocument();
    expect(screen.getByText("kharkevich")).toBeInTheDocument();
    expect(screen.getByText("Apache-2.0")).toBeInTheDocument();
  });

  it("says so rather than leaving a gap when a collection has no description", () => {
    render(
      <CollectionCard
        collection={collection({ description: "" })}
        installed={false}
        onView={vi.fn()}
        onInstall={vi.fn()}
      />,
    );
    expect(screen.getByText("No description available")).toBeInTheDocument();
  });

  it("omits the vendor and licence badges when the collection declares neither", () => {
    render(
      <CollectionCard
        collection={collection({ vendor: "", license: "" })}
        installed={false}
        onView={vi.fn()}
        onInstall={vi.fn()}
      />,
    );
    expect(screen.queryByText("kharkevich")).not.toBeInTheDocument();
    expect(screen.queryByText("Apache-2.0")).not.toBeInTheDocument();
    expect(screen.getByText("12 recipes")).toBeInTheDocument();
  });

  it("opens the collection when clicked", () => {
    const onView = vi.fn();
    render(
      <CollectionCard
        collection={collection()}
        installed={false}
        onView={onView}
        onInstall={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /spark-recipes/ }));
    expect(onView).toHaveBeenCalledTimes(1);
  });
});
