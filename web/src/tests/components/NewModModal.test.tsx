/** Creating a mod: what the modal insists on before it writes anything.
 *
 * A mod without an executable `run.sh` is a directory the runtime will pick
 * up and then do nothing with, which is worse than a mod that was never
 * created — so the modal refuses it up front rather than letting the operator
 * discover it at deploy time.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NewModModal from "@/components/NewModModal";

const ok = (body: unknown = {}) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);

const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

function renderModal(props: Partial<React.ComponentProps<typeof NewModModal>> = {}) {
  const onClose = vi.fn();
  const onSave = vi.fn().mockResolvedValue(undefined);
  const onError = vi.fn();
  const result = render(
    <NewModModal open onClose={onClose} onSave={onSave} onError={onError} {...props} />,
  );
  return { ...result, onClose, onSave, onError };
}

describe("NewModModal", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(ok({ id: "custom/x", name: "x" })));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders nothing while closed", () => {
    const { container } = renderModal({ open: false });
    expect(container).toBeEmptyDOMElement();
  });

  it("starts with a run.sh already stubbed out", () => {
    renderModal();
    expect(screen.getByRole("heading", { name: "New Mod" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("run.sh")).toBeInTheDocument();
  });

  it("cannot be submitted without a name", async () => {
    renderModal();
    expect(screen.getByRole("button", { name: /Create Mod/ })).toBeDisabled();
  });

  /** A mod whose run.sh is empty runs nothing; saying so here is cheaper than
   *  a deploy that silently applies no patch. */
  it("refuses a mod whose run.sh is empty", async () => {
    renderModal();

    await userEvent.type(screen.getByPlaceholderText("my-mod"), "kernel-patch");
    const body = screen.getAllByRole("textbox").find((el) => el.tagName === "TEXTAREA")!;
    await userEvent.clear(body);
    await userEvent.click(screen.getByRole("button", { name: /Create Mod/ }));

    expect(
      await screen.findByText("run.sh is required and cannot be empty"),
    ).toBeInTheDocument();
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("refuses a mod with no run.sh at all", async () => {
    renderModal();

    await userEvent.type(screen.getByPlaceholderText("my-mod"), "kernel-patch");
    await userEvent.clear(screen.getByDisplayValue("run.sh"));
    await userEvent.type(screen.getByPlaceholderText("filename"), "setup.py");
    await userEvent.click(screen.getByRole("button", { name: /Create Mod/ }));

    expect(
      await screen.findByText("run.sh is required and cannot be empty"),
    ).toBeInTheDocument();
  });

  it("writes every named file under a slug derived from the mod's name", async () => {
    const { onSave, onClose } = renderModal();

    await userEvent.type(screen.getByPlaceholderText("my-mod"), "Kernel Patch");
    await userEvent.click(screen.getByRole("button", { name: "+ Add File" }));
    const names = screen.getAllByPlaceholderText("filename");
    await userEvent.type(names[1], "notes.md");
    const bodies = screen.getAllByRole("textbox").filter((el) => el.tagName === "TEXTAREA");
    await userEvent.type(bodies[1], "hello");

    await userEvent.click(screen.getByRole("button", { name: /Create Mod/ }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const [url, init] = fetchMock().mock.calls[0];
    expect(url).toBe("/api/custom-files/mods/custom/kernel-patch");
    expect(init?.method).toBe("PUT");
    const written = JSON.parse(init?.body as string);
    expect(Object.keys(written)).toEqual(["run.sh", "notes.md"]);
    expect(written["notes.md"]).toBe("hello");
    expect(onSave).toHaveBeenCalledWith("custom/kernel-patch", "Kernel Patch");
    expect(onClose).toHaveBeenCalled();
  });

  it("drops a file the operator removed", async () => {
    const { onSave } = renderModal();

    await userEvent.type(screen.getByPlaceholderText("my-mod"), "mod");
    await userEvent.click(screen.getByRole("button", { name: "+ Add File" }));
    const names = screen.getAllByPlaceholderText("filename");
    expect(names).toHaveLength(2);

    // The second file's ✕ sits next to its own filename box.
    const remove = names[1].parentElement!.querySelector("button")!;
    await userEvent.click(remove);
    expect(screen.getAllByPlaceholderText("filename")).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: /Create Mod/ }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
  });

  it("surfaces the backend's reason for refusing the write", async () => {
    fetchMock().mockReturnValue(
      Promise.resolve({
        ok: false,
        status: 400,
        json: () => Promise.resolve({ detail: "a mod named kernel-patch already exists" }),
      } as Response),
    );
    const { onSave, onClose } = renderModal();

    await userEvent.type(screen.getByPlaceholderText("my-mod"), "kernel-patch");
    await userEvent.click(screen.getByRole("button", { name: /Create Mod/ }));

    expect(
      await screen.findByText("a mod named kernel-patch already exists"),
    ).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("reports a write that never reached the backend", async () => {
    fetchMock().mockRejectedValue(new Error("network down"));
    const { onError } = renderModal();

    await userEvent.type(screen.getByPlaceholderText("my-mod"), "mod");
    await userEvent.click(screen.getByRole("button", { name: /Create Mod/ }));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("network down"));
  });

  it("uploads a dropped ZIP and reports what the backend named it", async () => {
    fetchMock().mockReturnValue(ok({ id: "custom/bundle", name: "bundle" }));
    const { onSave, onClose } = renderModal();

    const zone = screen.getByText("Drag & drop a ZIP here").parentElement!;
    fireEvent.drop(zone, {
      dataTransfer: { files: [new File(["PK"], "bundle.zip", { type: "application/zip" })] },
    });

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("custom/bundle", "bundle"));
    expect(fetchMock().mock.calls[0][0]).toBe("/api/custom-files/mods/upload");
    expect(onClose).toHaveBeenCalled();
  });

  it("refuses anything that is not a ZIP", async () => {
    renderModal();

    const zone = screen.getByText("Drag & drop a ZIP here").parentElement!;
    fireEvent.drop(zone, {
      dataTransfer: { files: [new File(["x"], "mod.tar.gz", { type: "application/gzip" })] },
    });

    expect(await screen.findByText("Please upload a .zip file")).toBeInTheDocument();
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("refuses a ZIP too large to be a mod", async () => {
    const { onError } = renderModal();

    const zone = screen.getByText("Drag & drop a ZIP here").parentElement!;
    const huge = new File(["x".repeat(11 * 1024 * 1024)], "big.zip", { type: "application/zip" });
    fireEvent.drop(zone, { dataTransfer: { files: [huge] } });

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith("ZIP file too large (max 10MB)"),
    );
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("surfaces a rejected upload", async () => {
    fetchMock().mockReturnValue(
      Promise.resolve({
        ok: false,
        status: 400,
        json: () => Promise.resolve({ detail: "the archive has no run.sh" }),
      } as Response),
    );
    const { onError } = renderModal();

    const zone = screen.getByText("Drag & drop a ZIP here").parentElement!;
    fireEvent.drop(zone, {
      dataTransfer: { files: [new File(["PK"], "bundle.zip", { type: "application/zip" })] },
    });

    await waitFor(() => expect(onError).toHaveBeenCalledWith("the archive has no run.sh"));
  });

  it("highlights the drop zone while a file is over it", () => {
    renderModal();
    const zone = screen.getByText("Drag & drop a ZIP here").parentElement!;

    fireEvent.dragOver(zone);
    expect(screen.getByText("Drag & drop a ZIP here")).toHaveClass("text-primary");

    fireEvent.dragLeave(zone);
    expect(screen.getByText("Drag & drop a ZIP here")).toHaveClass("text-text-muted");
  });

  it("discards what was typed when the operator cancels", async () => {
    const { onClose } = renderModal();

    await userEvent.type(screen.getByPlaceholderText("my-mod"), "throwaway");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(onClose).toHaveBeenCalled();
    expect(fetchMock()).not.toHaveBeenCalled();
  });
});
