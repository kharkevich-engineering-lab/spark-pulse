/** Creating a recipe: validate first, then preview, then write.
 *
 * The modal never writes YAML it has not had the backend validate — a recipe
 * that parses wrong is a deploy that fails hours later — so the properties
 * held here are that a rejected file stops at the upload step with the
 * backend's reason attached, and that Save only exists on a clean preview.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import NewRecipeModal from "@/components/NewRecipeModal";

const YAML = "name: My Recipe\nmodel: Qwen/Qwen3-8B\ncontainer: vllm-node\n";

const ok = (body: unknown = {}) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) } as Response);

const rejected = (detail: string) =>
  Promise.resolve({
    ok: false,
    status: 400,
    json: () => Promise.resolve({ detail }),
  } as Response);

const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

function renderModal(props: Partial<React.ComponentProps<typeof NewRecipeModal>> = {}) {
  const onClose = vi.fn();
  const onSave = vi.fn().mockResolvedValue(undefined);
  const onError = vi.fn();
  const result = render(
    <NewRecipeModal open onClose={onClose} onSave={onSave} onError={onError} {...props} />,
  );
  return { ...result, onClose, onSave, onError };
}

const yamlFile = (content = YAML, name = "my-recipe.yaml") =>
  new File([content], name, { type: "text/yaml" });

const dropZone = () => screen.getByText(/Drag and drop a YAML file here/).parentElement!;

const dropOn = (file: File) => {
  fireEvent.drop(dropZone(), { dataTransfer: { files: [file] } });
};

describe("NewRecipeModal", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(ok()));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders nothing while closed", () => {
    const { container } = renderModal({ open: false });
    expect(container).toBeEmptyDOMElement();
  });

  it("opens on the upload step", () => {
    renderModal();
    expect(screen.getByRole("heading", { name: "Upload Recipe" })).toBeInTheDocument();
    expect(screen.getByText(/\.yaml or \.yml files supported/)).toBeInTheDocument();
  });

  it("takes the recipe's name from the YAML it validated", async () => {
    renderModal();

    dropOn(yamlFile());

    expect(await screen.findByRole("heading", { name: "Preview Recipe" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("My Recipe")).toBeInTheDocument();
    expect(screen.getByText("Source: my-recipe.yaml")).toBeInTheDocument();
    expect(fetchMock().mock.calls[0][0]).toBe("/api/custom-files/recipes/validate");
  });

  /** Without a `name:` the file's own name is the only thing left to call it,
   *  and an unnamed recipe cannot be saved at all. */
  it("falls back to the filename when the YAML names nothing", async () => {
    renderModal();

    dropOn(yamlFile("model: Qwen/Qwen3-8B\n", "fallback.yaml"));

    expect(await screen.findByDisplayValue("fallback")).toBeInTheDocument();
  });

  it("stops at the upload step with the backend's reason when the YAML is bad", async () => {
    fetchMock().mockReturnValue(rejected("line 2: mapping values are not allowed here"));
    renderModal();

    dropOn(yamlFile("name: x\n  bad\n"));

    expect(await screen.findByText("Validation failed")).toBeInTheDocument();
    expect(
      screen.getByText("line 2: mapping values are not allowed here"),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Upload Recipe" })).toBeInTheDocument();
  });

  it("refuses anything that is not YAML", async () => {
    renderModal();

    dropOn(new File(["{}"], "recipe.json", { type: "application/json" }));

    expect(await screen.findByText("Please upload a .yaml or .yml file")).toBeInTheDocument();
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("refuses a file too large to be a recipe", async () => {
    const { onError } = renderModal();

    dropOn(yamlFile("x".repeat(2 * 1024 * 1024)));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("File too large (max 1MB)"));
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("accepts a file chosen through the browse link", async () => {
    renderModal();

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, yamlFile());

    expect(await screen.findByRole("heading", { name: "Preview Recipe" })).toBeInTheDocument();
  });

  it("writes the previewed YAML under a slug of its name", async () => {
    const { onSave, onClose } = renderModal();

    dropOn(yamlFile());
    await screen.findByRole("heading", { name: "Preview Recipe" });
    await userEvent.click(screen.getByRole("button", { name: /Save Recipe/ }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    const [url, init] = fetchMock().mock.calls[1];
    expect(url).toBe("/api/custom-files/recipes/custom/my-recipe");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(init?.body as string)).toEqual({ content: YAML });
    expect(onSave).toHaveBeenCalledWith("custom/my-recipe", "My Recipe", YAML);
    expect(onClose).toHaveBeenCalled();
  });

  it("surfaces the backend's reason for refusing the write", async () => {
    renderModal();

    dropOn(yamlFile());
    await screen.findByRole("heading", { name: "Preview Recipe" });
    fetchMock().mockReturnValue(rejected("a recipe named my-recipe already exists"));
    await userEvent.click(screen.getByRole("button", { name: /Save Recipe/ }));

    expect(
      await screen.findByText("a recipe named my-recipe already exists"),
    ).toBeInTheDocument();
  });

  it("reports a write that never reached the backend", async () => {
    const { onError } = renderModal();

    dropOn(yamlFile());
    await screen.findByRole("heading", { name: "Preview Recipe" });
    fetchMock().mockRejectedValue(new Error("network down"));
    await userEvent.click(screen.getByRole("button", { name: /Save Recipe/ }));

    await waitFor(() => expect(onError).toHaveBeenCalledWith("network down"));
  });

  describe("manual entry", () => {
    it("starts from a template the operator can edit", async () => {
      renderModal();

      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));

      expect(screen.getByRole("heading", { name: "Manual Recipe" })).toBeInTheDocument();
      expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toContain(
        "name: My Custom Recipe",
      );
    });

    it("tracks the name as the operator types it into the YAML", async () => {
      renderModal();

      await userEvent.click(screen.getByRole("button", { name: "Manual" }));
      const editor = screen.getByRole("textbox");
      await userEvent.clear(editor);
      await userEvent.type(editor, "name: Typed Recipe");
      await userEvent.click(screen.getByRole("button", { name: "Validate recipe" }));

      expect(await screen.findByDisplayValue("Typed Recipe")).toBeInTheDocument();
    });

    it("shows the backend's complaint on the preview rather than saving", async () => {
      renderModal();

      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));
      fetchMock().mockReturnValue(rejected("model is required"));
      await userEvent.click(screen.getByRole("button", { name: "Validate recipe" }));

      expect(await screen.findByText("model is required")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /Save Recipe/ })).not.toBeInTheDocument();
    });

    it("reports a validation request that never landed", async () => {
      renderModal();

      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));
      fetchMock().mockRejectedValue(new Error("validator offline"));
      await userEvent.click(screen.getByRole("button", { name: "Validate recipe" }));

      expect(await screen.findByText("validator offline")).toBeInTheDocument();
    });

    /** Back from a manual preview returns to the editor with the YAML intact;
     *  dropping the operator out of the modal would lose what they typed. */
    it("goes back to the editor rather than closing", async () => {
      const { onClose } = renderModal();

      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));
      await userEvent.click(screen.getByRole("button", { name: "Validate recipe" }));
      await screen.findByRole("heading", { name: "Preview Recipe" });

      await userEvent.click(screen.getByRole("button", { name: "Back" }));

      expect(screen.getByRole("heading", { name: "Manual Recipe" })).toBeInTheDocument();
      expect(onClose).not.toHaveBeenCalled();
    });

    /** v2 is the format the engines are described in; v1 puts the whole
     *  launch into one vLLM-specific command template, so a recipe written
     *  that way can only ever run on vLLM. */
    it("starts from the v2 template", async () => {
      renderModal();

      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));

      const editor = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(editor.value).toContain('recipe_version: "2"');
      expect(editor.value).toContain("engine: vllm");
      expect(screen.getByRole("button", { name: "v2" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    });

    it("offers v1 for the recipes that are still written that way", async () => {
      renderModal();
      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));

      await userEvent.click(screen.getByRole("button", { name: "v1" }));

      const editor = screen.getByRole("textbox") as HTMLTextAreaElement;
      expect(editor.value).toContain("container: vllm-node");
      expect(editor.value).not.toContain('recipe_version: "2"');
    });

    /** Switching format replaces an untouched starter and nothing else: a
     *  click on a format button must not throw away what somebody wrote. */
    it("does not overwrite YAML the operator has edited", async () => {
      renderModal();
      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));
      const editor = screen.getByRole("textbox");
      await userEvent.clear(editor);
      await userEvent.type(editor, "name: Mine");

      await userEvent.click(screen.getByRole("button", { name: "v1" }));

      expect((editor as HTMLTextAreaElement).value).toBe("name: Mine");
    });

    /** One sentence tells you something is wrong; a field tells you where. */
    it("reports each problem against the field it belongs to", async () => {
      renderModal();
      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));
      fetchMock().mockReturnValue(
        Promise.resolve({
          ok: false,
          status: 400,
          json: () =>
            Promise.resolve({
              detail: {
                message: "Invalid recipe",
                errors: [
                  { path: "model", message: "field required" },
                  { path: "engines.vllm", message: "unknown engine" },
                ],
              },
            }),
        } as Response),
      );

      await userEvent.click(screen.getByRole("button", { name: "Validate recipe" }));

      expect(await screen.findByText("model: field required")).toBeInTheDocument();
      expect(screen.getByText("engines.vllm: unknown engine")).toBeInTheDocument();
    });

    it("still reads a plain-string reason", async () => {
      renderModal();
      await userEvent.click(screen.getByRole("button", { name: "Enter YAML manually" }));
      fetchMock().mockReturnValue(rejected("YAML content cannot be empty"));

      await userEvent.click(screen.getByRole("button", { name: "Validate recipe" }));

      expect(await screen.findByText("YAML content cannot be empty")).toBeInTheDocument();
    });

    it("cannot be previewed while the editor is empty", async () => {
      renderModal();

      await userEvent.click(screen.getByRole("button", { name: "Manual" }));
      await userEvent.clear(screen.getByRole("textbox"));

      expect(screen.getByRole("button", { name: "Validate recipe" })).toBeDisabled();
    });
  });

  it("closes from an upload-step preview rather than stepping back", async () => {
    const { onClose } = renderModal();

    dropOn(yamlFile());
    await screen.findByRole("heading", { name: "Preview Recipe" });
    await userEvent.click(screen.getByRole("button", { name: "Back" }));

    expect(onClose).toHaveBeenCalled();
  });

  it("closes when the operator cancels the upload step", async () => {
    const { onClose } = renderModal();
    await userEvent.click(screen.getAllByRole("button", { name: "Cancel" })[0]);
    expect(onClose).toHaveBeenCalled();
  });

  it("highlights the drop zone while a file is over it", () => {
    renderModal();
    const zone = dropZone();

    fireEvent.dragOver(zone);
    expect(zone).toHaveClass("border-primary");

    fireEvent.dragLeave(zone);
    expect(zone).not.toHaveClass("border-primary");
  });
});
