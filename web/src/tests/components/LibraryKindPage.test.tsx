/** The shape every Library page shares, and the one state vocabulary.
 *
 * What matters: the section is the URL hash (a link somebody sends lands on
 * Sources), the header's one primary action follows the section, only the
 * section on screen is mounted, and the filters mean the same on every page.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import LibraryKindPage, { sectionForHash } from "@/components/library/LibraryKindPage";
import {
  LibraryStateChip,
  libraryFilterTabs,
  matchesFilter,
  type LibraryState,
} from "@/components/library/LibraryState";
import { translatorFor } from "@/lib/i18n";

function Where() {
  const location = useLocation();
  return <span data-testid="where">{location.pathname + location.hash}</span>;
}

function renderAt(entry: string, get = vi.fn(), add = vi.fn()) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <LibraryKindPage
        title="Engines."
        action={{ label: "Pull image", onClick: get }}
        sourcesAction={{ label: "Add source", onClick: add }}
        sourcesCount={2}
        catalogue={<p>the catalogue</p>}
        sources={<p>the sources</p>}
      />
      <Where />
    </MemoryRouter>,
  );
  return { get, add };
}

describe("LibraryKindPage", () => {
  it("opens on the catalogue with the get action", () => {
    const { get } = renderAt("/engines");
    expect(screen.getByText("the catalogue")).toBeInTheDocument();
    expect(screen.queryByText("the sources")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Pull image" }));
    expect(get).toHaveBeenCalled();
  });

  it("opens on sources from the hash, with the add action", () => {
    const { add } = renderAt("/engines#sources");
    expect(screen.getByText("the sources")).toBeInTheDocument();
    expect(screen.queryByText("the catalogue")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    expect(add).toHaveBeenCalled();
  });

  it("puts the section in the address when a tab is chosen", () => {
    renderAt("/engines");
    fireEvent.click(screen.getByRole("tab", { name: /Sources/ }));
    expect(screen.getByTestId("where")).toHaveTextContent("/engines#sources");
    fireEvent.click(screen.getByRole("tab", { name: /Catalogue/ }));
    expect(screen.getByTestId("where")).toHaveTextContent(/^\/engines$/);
  });

  it("reads any other hash as the catalogue", () => {
    expect(sectionForHash("#sources")).toBe("sources");
    expect(sectionForHash("sources")).toBe("sources");
    expect(sectionForHash("#caches")).toBe("catalogue");
    expect(sectionForHash("")).toBe("catalogue");
  });

  it("renders no header action when the section has none", () => {
    render(
      <MemoryRouter initialEntries={["/mods#sources"]}>
        <LibraryKindPage title="Mods." catalogue={<p>c</p>} sources={<p>s</p>} />
      </MemoryRouter>,
    );
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});

describe("the state vocabulary", () => {
  const { t } = translatorFor("en");
  const states: LibraryState[] = ["on_nodes", "partial", "available", "available", "update", "local"];

  it("counts each filter the same way on every page", () => {
    const tabs = libraryFilterTabs(t, states);
    expect(tabs.map((tab) => [tab.id, tab.count])).toEqual([
      ["all", 6],
      ["on_nodes", 4],
      ["available", 2],
      ["updates", 1],
    ]);
  });

  it("leaves out a filter a kind cannot offer", () => {
    expect(libraryFilterTabs(t, states, ["all", "on_nodes"]).map((tab) => tab.id)).toEqual([
      "all",
      "on_nodes",
    ]);
  });

  it("counts anything held as on nodes, and only the unheld as available", () => {
    expect(matchesFilter("partial", "on_nodes")).toBe(true);
    expect(matchesFilter("update", "on_nodes")).toBe(true);
    expect(matchesFilter("unknown", "on_nodes")).toBe(true);
    expect(matchesFilter("available", "on_nodes")).toBe(false);
    expect(matchesFilter("available", "available")).toBe(true);
    expect(matchesFilter("update", "updates")).toBe(true);
    expect(matchesFilter("on_nodes", "updates")).toBe(false);
  });

  it("shows the state, its detail and why", () => {
    render(<LibraryStateChip state="partial" detail="1 of 2 nodes" title="spark-02 has none" />);
    const chip = screen.getByTestId("library-state-partial");
    expect(chip).toHaveTextContent("Partial");
    expect(chip).toHaveTextContent("1 of 2 nodes");
    expect(chip).toHaveAttribute("title", "spark-02 has none");
  });

  it("calls a held recipe or mod installed, not on nodes", () => {
    render(<LibraryStateChip state="on_nodes" heldAs="installed" />);
    expect(screen.getByTestId("library-state-on_nodes")).toHaveTextContent("Installed");
    expect(libraryFilterTabs(t, states, ["on_nodes"], "installed")[0].label).toBe("Installed");
  });

  it("shows no detail when there is none", () => {
    render(<LibraryStateChip state="available" detail="" />);
    expect(screen.getByTestId("library-state-available").textContent).toBe("Available");
  });
});
