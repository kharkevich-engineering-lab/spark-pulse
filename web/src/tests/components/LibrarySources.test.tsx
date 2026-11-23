/** The Sources section of Engines and Models, and the standing a row reports.
 *
 * What matters: an edit is held until the section's own save bar writes it,
 * the bar is there only while something is unsaved, an engine index forced by
 * the environment cannot be edited, and a node that could not be asked is
 * *unknown*, never counted as without a copy.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { translatorFor } from "@/lib/i18n";
import EngineSources from "@/components/library/EngineSources";
import ModelSources from "@/components/library/ModelSources";
import { engineStanding } from "@/components/library/EnginesTab";
import { modelStanding } from "@/components/library/ModelsTab";
import type { ImageEntry, ImagePresence } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchEngines: vi.fn(),
  fetchSettings: vi.fn(),
  refreshEngines: vi.fn(),
  updateSettings: vi.fn(),
  fetchModelSources: vi.fn(),
  saveModelSources: vi.fn(),
}));

import {
  fetchEngines,
  fetchModelSources,
  fetchSettings,
  refreshEngines,
  saveModelSources,
  updateSettings,
} from "@/lib/api";

const { t } = translatorFor("en");

const REGISTRY = {
  default_engine: "vllm",
  engines: [{ engine: "vllm" }, { engine: "sglang" }],
  indexes: [
    { ref: "idx-a", engines: 3, fetched_at: 1_700_000_000, error: null },
    { ref: "idx-b", engines: 0, fetched_at: null, error: "boom" },
  ],
  default_indexes: ["idx-a"],
  indexes_env_managed: false,
};

describe("EngineSources", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchEngines).mockResolvedValue(REGISTRY as never);
    vi.mocked(fetchSettings).mockResolvedValue({
      default_engine: "vllm",
      engine_indexes: ["idx-a", "idx-b"],
      engine_index_cache_ttl_seconds: 600,
    } as never);
    vi.mocked(updateSettings).mockResolvedValue({} as never);
    vi.mocked(refreshEngines).mockResolvedValue({} as never);
  });

  it("lists each index with what it last published, and the failure of one that did not", async () => {
    render(<EngineSources />);

    expect(await screen.findByLabelText("Index 1")).toHaveValue("idx-a");
    expect(screen.getByLabelText("Index 2")).toHaveValue("idx-b");
    expect(screen.getByText(/3 engines, fetched/)).toBeInTheDocument();
    expect(screen.getByText(/not fetched yet/)).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByLabelText("Index cache lifetime")).toHaveValue(600);
    expect(screen.queryByRole("region", { name: "Save changes" })).toBeNull();
  });

  it("holds an edit until saved, then writes only its own keys", async () => {
    const user = userEvent.setup();
    render(<EngineSources />);

    await user.type(await screen.findByLabelText("Index 2"), "2");
    await user.selectOptions(screen.getByLabelText("Default engine"), "sglang");
    fireEvent.change(screen.getByLabelText("Index cache lifetime"), { target: { value: "30" } });
    expect(updateSettings).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith({
        default_engine: "sglang",
        engine_indexes: ["idx-a", "idx-b2"],
        engine_index_cache_ttl_seconds: 30,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("region", { name: "Save changes" })).toBeNull());
  });

  it("discards an edit, ignores a blank line, trims a typed one", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<EngineSources addTick={0} />);
    await user.click(await screen.findByLabelText("Remove index 2"));
    expect(screen.getByRole("region", { name: "Save changes" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("Index 2")).toBeInTheDocument();

    rerender(<EngineSources addTick={1} />);
    expect(await screen.findByLabelText("Index 3")).toHaveValue("");
    // A blank line is not an index, so it is not a change either.
    expect(screen.queryByRole("region", { name: "Save changes" })).toBeNull();
    await user.type(screen.getByLabelText("Index 3"), "  idx-c ");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(updateSettings).toHaveBeenCalledWith(
        expect.objectContaining({ engine_indexes: ["idx-a", "idx-b", "idx-c"] }),
      ),
    );
  });

  it("says why a save failed and keeps the edit", async () => {
    const user = userEvent.setup();
    vi.mocked(updateSettings).mockRejectedValue(new Error("settings refused"));
    render(<EngineSources />);

    await user.click(await screen.findByLabelText("Remove index 2"));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("settings refused")).toBeInTheDocument();
    expect(screen.queryByLabelText("Index 2")).toBeNull();
  });

  it("offers the default index when the list has moved off it", async () => {
    const user = userEvent.setup();
    render(<EngineSources />);

    await user.click(await screen.findByRole("button", { name: "Use the default index" }));
    expect(screen.getByLabelText("Index 1")).toHaveValue("idx-a");
    expect(screen.queryByLabelText("Index 2")).toBeNull();
    expect(screen.queryByRole("button", { name: "Use the default index" })).toBeNull();
  });

  it("refreshes the index, and reports a refresh that failed", async () => {
    const user = userEvent.setup();
    render(<EngineSources />);

    await user.click(await screen.findByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(refreshEngines).toHaveBeenCalled());

    vi.mocked(refreshEngines).mockRejectedValue(new Error("offline"));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("offline")).toBeInTheDocument();
  });

  it("does not let an index the environment decides be edited or added", async () => {
    vi.mocked(fetchEngines).mockResolvedValue({ ...REGISTRY, indexes_env_managed: true } as never);
    const { rerender } = render(<EngineSources addTick={0} />);

    expect(await screen.findByTestId("indexes-from-env")).toBeInTheDocument();
    expect(screen.getByLabelText("Index 1")).toBeDisabled();
    expect(screen.getByLabelText("Remove index 1")).toBeDisabled();
    rerender(<EngineSources addTick={1} />);
    expect(screen.queryByLabelText("Index 3")).toBeNull();
  });

  it("types the default engine when no index publishes one yet, and says there are no indexes", async () => {
    vi.mocked(fetchEngines).mockResolvedValue({ default_engine: "", engines: [] } as never);
    vi.mocked(fetchSettings).mockResolvedValue({} as never);
    render(<EngineSources />);

    expect(await screen.findByText("No indexes configured.")).toBeInTheDocument();
    expect(screen.getByLabelText("Default engine")).toHaveAttribute("placeholder", "vllm");
    expect(screen.getByLabelText("Index cache lifetime")).toHaveValue(3600);
  });
});

describe("ModelSources", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchModelSources).mockResolvedValue([
      { name: "hf", type: "hf_hub", endpoint: "https://huggingface.co", token_secret: "" },
      { name: "disk", type: "local_path", path: "/models" },
    ]);
    vi.mocked(saveModelSources).mockResolvedValue([]);
  });

  it("shows each source by its type and saves an edit through the sources endpoint", async () => {
    const user = userEvent.setup();
    render(<ModelSources />);

    expect(await screen.findByLabelText("Source 1 endpoint")).toHaveValue("https://huggingface.co");
    expect(screen.getByLabelText("Source 2 path")).toHaveValue("/models");
    expect(screen.queryByRole("region", { name: "Save changes" })).toBeNull();

    await user.type(screen.getByLabelText("Source 1 token secret"), "hf-key");
    await user.selectOptions(screen.getByLabelText("Source 2 type"), "hf_hub");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(saveModelSources).toHaveBeenCalled());
    const sent = vi.mocked(saveModelSources).mock.calls[0][0];
    expect(sent[0]).toMatchObject({ name: "hf", token_secret: "hf-key" });
    expect(sent[1]).toMatchObject({ name: "disk", type: "hf_hub" });
    await waitFor(() => expect(screen.queryByRole("region", { name: "Save changes" })).toBeNull());
  });

  it("adds a blank hub source, removes one, and discards both", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ModelSources addTick={0} />);
    await screen.findByLabelText("Source 1 name");

    rerender(<ModelSources addTick={1} />);
    expect(await screen.findByLabelText("Source 3 name")).toHaveValue("");
    await user.click(screen.getByLabelText("Remove source 1"));
    expect(screen.queryByLabelText("Source 3 name")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("Source 2 name")).toHaveValue("disk");
    expect(screen.queryByLabelText("Source 3 name")).toBeNull();
  });

  it("says when there are none, and why a save failed", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchModelSources).mockResolvedValue([]);
    vi.mocked(saveModelSources).mockRejectedValue(new Error("name taken"));
    const { rerender } = render(<ModelSources addTick={0} />);

    expect(await screen.findByText("No sources configured.")).toBeInTheDocument();
    rerender(<ModelSources addTick={1} />);
    await user.click(await screen.findByRole("button", { name: "Save" }));
    expect(await screen.findByText("name taken")).toBeInTheDocument();
  });
});

describe("what a row stands at", () => {
  const image = (over: Partial<ImageEntry> = {}) =>
    ({ ref: "r", present: true, digest_drift: false, ...over }) as ImageEntry;
  const presence = (nodes: Partial<ImagePresence["nodes"][number]>[]) =>
    ({ ref: "r", local: true, image_id: "", nodes }) as unknown as ImagePresence;

  it("reads an image from this machine's copy, then from what every node said", () => {
    const row = (over?: Partial<ImageEntry>) => ({ image: image(over), engine: null });
    expect(engineStanding(row(), undefined, true, t).state).toBe("getting");
    expect(engineStanding(row({ present: false }), undefined, false, t).state).toBe("available");
    expect(engineStanding(row({ digest_drift: true }), undefined, false, t).state).toBe("update");
    expect(engineStanding(row(), undefined, false, t).state).toBe("on_nodes");
    expect(engineStanding(row(), "loading", false, t).state).toBe("on_nodes");
    expect(engineStanding(row(), presence([{ node: "b", present: true }]), false, t).state).toBe("on_nodes");
    expect(engineStanding(row(), presence([{ node: "b", present: false }]), false, t)).toMatchObject({
      state: "partial",
      detail: "1 of 2 nodes",
    });
    expect(
      engineStanding(row(), presence([{ node: "b", present: false, error: "down" }]), false, t),
    ).toMatchObject({ state: "unknown", title: "b: down" });
  });

  it("keeps the no-image note on an image nobody has published", () => {
    const standing = engineStanding(
      { image: image({ present: false }), engine: { available: false } as never },
      undefined,
      false,
      t,
    );
    expect(standing.detail).toBe("no image published");
  });

  it("reads a model from its per-node verdict, a download first", () => {
    const entries = [{ node: "a", state: "verified" }];
    expect(modelStanding({ state: "ok", label: "1 of 1 nodes" }, entries, { percent: 40 })).toEqual({
      state: "getting",
      detail: "40%",
    });
    expect(modelStanding({ state: "ok", label: "x" }, entries, { percent: null })).toEqual({ state: "getting" });
    expect(modelStanding({ state: "unknown", label: "not checked" }, null, null).state).toBe("unknown");
    expect(modelStanding({ state: "ok", label: "1 of 1 nodes" }, entries, null).state).toBe("on_nodes");
    expect(modelStanding({ state: "warn", label: "a only" }, entries, null).state).toBe("partial");
    expect(modelStanding({ state: "bad", label: "partial on a" }, entries, null).state).toBe("partial");
    expect(
      modelStanding({ state: "warn", label: "a only" }, [...entries, { node: "b", error: "down" }], null).state,
    ).toBe("unknown");
    expect(modelStanding({ state: "unknown", label: "?" }, entries, null).state).toBe("unknown");
  });
});
