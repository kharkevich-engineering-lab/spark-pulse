/** Mods: bundled and custom in one table, a source filter and a search.
 *
 * What the page owes: a bundled mod opens read-only so its script can be read
 * before a recipe runs it as root; a custom one opens in its editor and saves
 * every file together; the first custom mod can be created from an empty
 * page; and each list — bundled and custom — may fail without hiding the
 * other.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ModsPage from "@/pages/ModsPage";
import type { CustomModInfo, ModSummary } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchMods: vi.fn(),
  fetchMod: vi.fn(),
  listCustomMods: vi.fn(),
  getCustomModFiles: vi.fn(),
  saveCustomModFiles: vi.fn(),
  deleteCustomMod: vi.fn(),
}));

import {
  deleteCustomMod,
  fetchMod,
  fetchMods,
  getCustomModFiles,
  listCustomMods,
  saveCustomModFiles,
} from "@/lib/api";

const MOD: ModSummary = {
  id: "flash-attn",
  description: "Swap in the fused attention kernel",
  files: [{ name: "run.sh", kind: "script" }],
  has_patches: true,
};

const CUSTOM_MOD: CustomModInfo = {
  id: "custom/my-mod",
  name: "My Mod",
  description: "does a thing",
  filepath: "/cfg/mods/my-mod",
  has_run_sh: true,
};

const renderPage = (entry = "/mods") =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <ModsPage />
    </MemoryRouter>,
  );

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(fetchMods).mockResolvedValue([MOD]);
  vi.mocked(listCustomMods).mockResolvedValue([CUSTOM_MOD]);
  vi.mocked(fetchMod).mockResolvedValue({ ...MOD, script: "#!/bin/bash\necho patched\n" });
  vi.mocked(getCustomModFiles).mockResolvedValue({ files: { "run.sh": "#!/bin/bash\n" }, id: CUSTOM_MOD.id });
  vi.mocked(saveCustomModFiles).mockResolvedValue({ saved: true });
  vi.mocked(deleteCustomMod).mockResolvedValue({ deleted: true });
});

describe("ModsPage", () => {
  it("says what each mod is in the library's words", async () => {
    renderPage();
    await screen.findByText("My Mod");

    // Bundled is installed; one of the operator's own is local.
    expect(screen.getAllByTestId("library-state-on_nodes")).toHaveLength(1);
    expect(screen.getAllByTestId("library-state-local")).toHaveLength(1);
    expect(screen.getByText("Installed")).toBeInTheDocument();
  });

  it("has a catalogue and read-only sources, and only New mod to press", async () => {
    renderPage("/mods#sources");

    const table = await screen.findByTestId("mod-sources-table");
    expect(within(table).getByTestId("mod-source-bundled")).toHaveTextContent("1");
    expect(within(table).getByTestId("mod-source-custom")).toHaveTextContent("~/.config/spark-pulse/custom-mods");
    expect(screen.getByRole("tab", { name: /^Sources \(2\)/ })).toBeInTheDocument();
    // Nothing to add, enable or remove here.
    expect(screen.queryByRole("button", { name: "New mod" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Add source|Remove|Disable/ })).toBeNull();
    expect(screen.queryByTestId("mods-table")).toBeNull();

    await userEvent.click(screen.getByRole("tab", { name: /^Catalogue/ }));
    expect(await screen.findByTestId("mods-table")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New mod" })).toBeInTheDocument();
  });

  it("lists bundled and custom mods in one table, with their files", async () => {
    renderPage();
    expect(await screen.findByRole("heading", { level: 1, name: "Mods." })).toBeInTheDocument();
    const table = await screen.findByTestId("mods-table");
    await within(table).findByText("My Mod");
    expect(within(table).getByText("flash-attn")).toBeInTheDocument();
    expect(within(table).getByText("Swap in the fused attention kernel")).toBeInTheDocument();
    expect(within(table).getByText("patches")).toBeInTheDocument();
    expect(within(table).getAllByText("run.sh")).toHaveLength(2);
    expect(screen.getByText("2 mods")).toBeInTheDocument();
  });

  it("narrows by source and by search", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("My Mod");

    await user.click(screen.getByRole("tab", { name: /^Bundled/ }));
    expect(screen.queryByText("My Mod")).toBeNull();
    expect(screen.getByText("flash-attn")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /^Custom/ }));
    expect(screen.queryByText("flash-attn")).toBeNull();

    await user.click(screen.getByRole("tab", { name: /^All/ }));
    await user.type(screen.getByRole("searchbox", { name: "Search mods" }), "kernel");
    expect(screen.queryByText("My Mod")).toBeNull();
    await user.type(screen.getByRole("searchbox", { name: "Search mods" }), "zzz");
    expect(screen.getByText("Nothing matches.")).toBeInTheDocument();
  });

  it("says there are no mods rather than showing an empty table", async () => {
    vi.mocked(fetchMods).mockResolvedValue([]);
    vi.mocked(listCustomMods).mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText("No mods found")).toBeInTheDocument();
  });

  it("says there are no custom mods, and still offers to create the first", async () => {
    const user = userEvent.setup();
    vi.mocked(listCustomMods).mockResolvedValue([]);
    renderPage();
    await screen.findByText("flash-attn");
    await user.click(screen.getByRole("tab", { name: /^Custom/ }));
    expect(screen.getByText("No custom mods")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /New mod/ })).toBeInTheDocument();
  });

  /** A mod is a shell script that runs inside the container as root; the
   *  drawer exists so an operator can read it before applying it. */
  it("opens a bundled mod and shows the script it would run, and copies it", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "flash-attn" }));

    await waitFor(() => expect(fetchMod).toHaveBeenCalledWith("flash-attn"));
    expect(await screen.findByText(/echo patched/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Copy/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("#!/bin/bash\necho patched\n"));
    expect(await screen.findByText("Copied")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText(/echo patched/)).toBeNull();
    vi.unstubAllGlobals();
  });

  it("reports a bundled mod whose script could not be read", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchMod).mockRejectedValue(new Error("mod dir vanished"));
    renderPage();
    await user.click(await screen.findByText("flash-attn"));
    expect(await screen.findByText(/mod dir vanished/)).toBeInTheDocument();
  });

  it("opens a custom mod with the files it holds and saves them together", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByText("My Mod"));

    await waitFor(() => expect(getCustomModFiles).toHaveBeenCalledWith("custom/my-mod"));
    const editor = await screen.findByRole("textbox");
    await user.clear(editor);
    await user.type(editor, "#!/bin/sh");
    await user.click(screen.getByRole("button", { name: /Save/ }));

    await waitFor(() => expect(saveCustomModFiles).toHaveBeenCalledWith("custom/my-mod", { "run.sh": "#!/bin/sh" }));
  });

  it("reports a custom mod whose files could not be read", async () => {
    const user = userEvent.setup();
    vi.mocked(getCustomModFiles).mockRejectedValue(new Error("gone"));
    renderPage();
    await user.click(await screen.findByText("My Mod"));
    expect(await screen.findByText("Failed to load mod")).toBeInTheDocument();
  });

  it("deletes a custom mod from its editor once the operator confirms", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByText("My Mod"));
    await screen.findByRole("textbox");
    await user.click(screen.getByRole("button", { name: /^Delete$/ }));
    await user.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);
    await waitFor(() => expect(deleteCustomMod).toHaveBeenCalledWith("custom/my-mod"));
  });

  it("deletes a custom mod from its row, after confirming, and says why one failed", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Delete My Mod" }));
    expect(deleteCustomMod).not.toHaveBeenCalled();
    await user.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);
    await waitFor(() => expect(deleteCustomMod).toHaveBeenCalledWith("custom/my-mod"));

    vi.mocked(deleteCustomMod).mockRejectedValueOnce(new Error("permission denied"));
    await user.click(await screen.findByRole("button", { name: "Delete My Mod" }));
    await user.click(screen.getAllByRole("button", { name: "Delete" }).at(-1)!);
    expect(await screen.findByText("permission denied")).toBeInTheDocument();
  });

  it("deletes nothing when the confirmation is cancelled, and closes an alert", async () => {
    const user = userEvent.setup();
    vi.mocked(getCustomModFiles).mockRejectedValue(new Error("gone"));
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Delete My Mod" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(deleteCustomMod).not.toHaveBeenCalled();

    await user.click(screen.getByText("My Mod"));
    await screen.findByText("Failed to load mod");
    await user.click(screen.getByRole("button", { name: "OK" }));
    await waitFor(() => expect(screen.queryByText("Failed to load mod")).toBeNull());
  });

  it("re-reads the custom directory after a new mod is written", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ id: "custom/m", name: "m" }) }),
    );
    renderPage();
    await screen.findByText("My Mod");
    const before = vi.mocked(listCustomMods).mock.calls.length;

    await user.click(screen.getByRole("button", { name: /New mod/ }));
    await user.type(screen.getByPlaceholderText("my-mod"), "fresh");
    await user.click(screen.getByRole("button", { name: /Create mod/ }));

    await waitFor(() => expect(vi.mocked(listCustomMods).mock.calls.length).toBeGreaterThan(before));
    expect(screen.queryByRole("heading", { name: "New mod" })).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("abandons the new-mod form without writing anything", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /New mod/ }));
    expect(screen.getByRole("heading", { name: "New mod" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("heading", { name: "New mod" })).not.toBeInTheDocument();
  });

  it("surfaces each list that could not be read, keeping the other", async () => {
    vi.mocked(listCustomMods).mockRejectedValue(new Error("config dir missing"));
    renderPage();
    expect(await screen.findByText("config dir missing")).toBeInTheDocument();
    expect(screen.getByText("flash-attn")).toBeInTheDocument();
  });
});
