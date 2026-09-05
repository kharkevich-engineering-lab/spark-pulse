/** RegistryCard: one OCI registry, its reachability and the actions on it.
 *
 * The rule that matters is that the default registry cannot be removed —
 * losing it would leave an install with nowhere to fetch recipes from and no
 * obvious way to get it back — so the remove button is absent rather than
 * disabled.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import RegistryCard from "@/components/RegistryCard";
import type { OciRegistry } from "@/lib/types";

const registry = (over: Partial<OciRegistry> = {}): OciRegistry => ({
  name: "ghcr",
  url: "ghcr.io/kharkevich-engineering-lab",
  enabled: true,
  default: false,
  auth_type: "none",
  connected: true,
  ...over,
});

const handlers = () => ({
  onToggle: vi.fn(),
  onTest: vi.fn(),
  onRemove: vi.fn(),
});

describe("RegistryCard", () => {
  it("names the registry and the url it points at", () => {
    render(<RegistryCard reg={registry()} {...handlers()} />);
    expect(screen.getByText("ghcr")).toBeInTheDocument();
    expect(screen.getByText("ghcr.io/kharkevich-engineering-lab")).toBeInTheDocument();
  });

  it("marks the default registry", () => {
    render(<RegistryCard reg={registry({ default: true })} {...handlers()} />);
    expect(screen.getByText("default")).toBeInTheDocument();
  });

  it("enables and disables a registry", () => {
    const h = handlers();
    const { rerender } = render(<RegistryCard reg={registry()} {...h} />);

    fireEvent.click(screen.getByTitle("Disable"));
    expect(h.onToggle).toHaveBeenCalledTimes(1);

    rerender(<RegistryCard reg={registry({ enabled: false })} {...h} />);
    expect(screen.getByTitle("Enable")).toBeInTheDocument();
  });

  /** A registry we have not reached yet is retried by hand; one we have is
   *  not, because the button would be a no-op. */
  it("offers a connection test only while the registry is unreached", () => {
    const h = handlers();
    const { rerender } = render(<RegistryCard reg={registry()} {...h} />);
    expect(screen.getByTitle("Test connection")).toBeDisabled();

    rerender(<RegistryCard reg={registry({ connected: false })} {...h} />);
    const test = screen.getByTitle("Test connection");
    expect(test).toBeEnabled();
    fireEvent.click(test);
    expect(h.onTest).toHaveBeenCalledTimes(1);
  });

  it("removes a registry that is not the default", () => {
    const h = handlers();
    render(<RegistryCard reg={registry()} {...h} />);

    fireEvent.click(screen.getByTitle("Remove registry"));
    expect(h.onRemove).toHaveBeenCalledTimes(1);
  });

  it("offers no way to remove the default registry", () => {
    render(<RegistryCard reg={registry({ default: true })} {...handlers()} />);
    expect(screen.queryByTitle("Remove registry")).not.toBeInTheDocument();
  });

  it("says nothing about versions when none are known", () => {
    render(<RegistryCard reg={registry()} {...handlers()} />);
    expect(screen.queryByText(/versions/)).not.toBeInTheDocument();
  });

  it("counts the versions it holds and opens a picker for them", () => {
    const onVersionChange = vi.fn();
    render(
      <RegistryCard
        reg={registry()}
        versions={["1.0.0", "1.1.0", "2.0.0"]}
        onVersionChange={onVersionChange}
        {...handlers()}
      />,
    );

    // Collapsed until asked: three versions is a footnote, not the headline.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /3 versions/ }));

    const picker = screen.getByRole("combobox");
    expect(picker).toBeInTheDocument();
    fireEvent.change(picker, { target: { value: "2.0.0" } });
    expect(onVersionChange).toHaveBeenCalledWith("2.0.0");
    expect(picker).toHaveValue("2.0.0");
  });

  it("collapses the version list again", () => {
    render(<RegistryCard reg={registry()} versions={["1.0.0"]} {...handlers()} />);

    fireEvent.click(screen.getByRole("button", { name: /1 versions/ }));
    expect(screen.getByRole("combobox")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /1 versions/ }));
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("selects a version even when no page is listening", () => {
    render(<RegistryCard reg={registry()} versions={["1.0.0"]} {...handlers()} />);

    fireEvent.click(screen.getByRole("button", { name: /1 versions/ }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "1.0.0" } });
    expect(screen.getByRole("combobox")).toHaveValue("1.0.0");
  });
});
