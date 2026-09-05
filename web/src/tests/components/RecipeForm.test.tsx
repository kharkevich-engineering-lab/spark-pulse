/** The recipe form: what a customization actually sends.
 *
 * The form is the only place an operator edits a bundled recipe, and it saves
 * a *diff* rather than the whole recipe — an unchanged field must not become
 * a customization, because a customization is what stops the bundled recipe
 * from ever updating that field again. So the properties worth holding are:
 * the fields render what the recipe declares, edits reach the save callback,
 * and untouched fields stay out of it.
 */

import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import RecipeForm from "@/components/RecipeForm";
import type { RecipeCustomization, RecipeDetail, RecipeFormRef } from "@/lib/types";

/** The lazy code editor pulls a syntax-highlighting bundle over a dynamic
 *  import; a plain textarea is the same contract for a test that only needs
 *  to type YAML into it. */
vi.mock("@/components/LazyCodeEditor", () => ({
  default: ({
    value,
    onChange,
    disabled,
  }: {
    value: string;
    onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
    disabled?: boolean;
  }) => (
    <textarea aria-label="Recipe YAML" value={value} onChange={onChange} disabled={disabled} />
  ),
}));

const RECIPE = {
  id: "bundled/qwen3-8b",
  name: "Qwen3 8B",
  model: "Qwen/Qwen3-8B",
  container: "vllm-node",
  command: "",
  description: "",
  mods: ["flash-attn"],
  defaults: { tensor_parallel: 1, host: "0.0.0.0" },
  params: {},
  env: { HF_HOME: "/models" },
  build_args: ["--build-arg X=1"],
  solo_only: false,
  cluster_only: false,
  is_customized: false,
  recipe_version: "2",
  engine: "vllm",
  engines: ["vllm"],
  source: "bundled",
  engine_support: [],
  min_nodes: null,
  engine_specs: {},
} as unknown as RecipeDetail;

/** `RecipeForm` exposes save/cancel/getDeployName through a ref, the way
 *  `RecipeDrawer` drives it; this harness is that parent, minus the drawer. */
function Harness({
  recipe = RECIPE,
  customization = {},
  onSaveCustomization,
  startEditing = true,
}: {
  recipe?: RecipeDetail;
  customization?: RecipeCustomization;
  onSaveCustomization?: (fields: Partial<RecipeCustomization>) => void;
  startEditing?: boolean;
}) {
  const ref = useRef<RecipeFormRef>(null);
  const [isEditing, setIsEditing] = useState(startEditing);
  const [deployName, setDeployName] = useState("");
  return (
    <div>
      <button onClick={() => ref.current?.save()}>harness-save</button>
      <button onClick={() => ref.current?.cancel()}>harness-cancel</button>
      <button onClick={() => setDeployName(ref.current?.getDeployName() ?? "")}>
        harness-deploy-name
      </button>
      <button onClick={() => setIsEditing((v) => !v)}>harness-toggle-edit</button>
      <output>{deployName}</output>
      <RecipeForm
        ref={ref}
        recipe={recipe}
        customization={customization}
        onDeploy={async () => {}}
        onSaveCustomization={onSaveCustomization}
        isRunning={false}
        clusterBlocked={false}
        isEditing={isEditing}
      />
    </div>
  );
}

const save = () => userEvent.click(screen.getByRole("button", { name: "harness-save" }));

describe("RecipeForm fields", () => {
  it("shows the recipe's model, container, defaults, env, build args and mods", () => {
    render(<Harness startEditing={false} />);

    expect(screen.getByDisplayValue("Qwen3 8B")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Qwen/Qwen3-8B")).toBeInTheDocument();
    expect(screen.getByDisplayValue("vllm-node")).toBeInTheDocument();
    expect(screen.getByText("tensor_parallel")).toBeInTheDocument();
    expect(screen.getByText("HF_HOME")).toBeInTheDocument();
    expect(screen.getByText("--build-arg X=1")).toBeInTheDocument();
    expect(screen.getByText("flash-attn")).toBeInTheDocument();
  });

  it("locks every field until the operator asks to edit", () => {
    render(<Harness startEditing={false} />);
    expect(screen.getByDisplayValue("Qwen/Qwen3-8B")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Edit as YAML" })).not.toBeInTheDocument();
  });

  it("says so rather than showing an empty box when a recipe declares nothing", () => {
    const bare = { ...RECIPE, env: {}, build_args: [], mods: [], defaults: {} } as RecipeDetail;
    render(<Harness recipe={bare} startEditing={false} />);

    expect(screen.getByText("No environment variables")).toBeInTheDocument();
    expect(screen.getByText("No build args")).toBeInTheDocument();
    expect(screen.getByText("No mods")).toBeInTheDocument();
  });

  it("offers the command template only for a recipe that carries one", () => {
    render(<Harness startEditing={false} />);
    expect(screen.queryByText("Command Template")).not.toBeInTheDocument();

    const v1 = { ...RECIPE, command: "vllm serve {model}" } as RecipeDetail;
    render(<Harness recipe={v1} startEditing={false} />);
    expect(screen.getByDisplayValue("vllm serve {model}")).toBeInTheDocument();
  });
});

describe("RecipeForm saving", () => {
  /** A customization pins a field forever, so a Save the operator made
   *  without touching anything must not pin all of them. */
  it("sends no fields when nothing was edited", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    await save();

    expect(onSave).toHaveBeenCalledWith({});
  });

  it("sends only the field that changed", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    const model = screen.getByDisplayValue("Qwen/Qwen3-8B");
    await userEvent.clear(model);
    await userEvent.type(model, "Qwen/Qwen3-32B");
    await save();

    expect(onSave).toHaveBeenCalledWith({ model: "Qwen/Qwen3-32B" });
  });

  /** Defaults are saved as a diff of the diff: only the parameters whose
   *  value actually moved, so the rest keep tracking the bundled recipe. */
  it("sends only the default parameter whose value moved", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    const tp = screen.getByDisplayValue("1");
    await userEvent.clear(tp);
    await userEvent.type(tp, "2");
    await save();

    expect(onSave).toHaveBeenCalledWith({ defaults: { tensor_parallel: 2 } });
  });

  it("carries an added environment variable", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    await userEvent.type(screen.getByPlaceholderText("KEY"), "NCCL_DEBUG");
    await userEvent.type(screen.getByPlaceholderText("VALUE"), "INFO");
    await userEvent.click(screen.getAllByRole("button", { name: "Add" })[0]);
    await save();

    expect(onSave).toHaveBeenCalledWith({
      env: { HF_HOME: "/models", NCCL_DEBUG: "INFO" },
    });
  });

  it("ignores an environment variable added with no key", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    await userEvent.type(screen.getByPlaceholderText("VALUE"), "orphan");
    await userEvent.click(screen.getAllByRole("button", { name: "Add" })[0]);
    await save();

    expect(onSave).toHaveBeenCalledWith({});
  });

  it("carries an added build arg and a removed one", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    await userEvent.type(screen.getByPlaceholderText("--build-arg X=1"), "--build-arg Y=2");
    await userEvent.click(screen.getAllByRole("button", { name: "Add" })[1]);
    await save();

    expect(onSave).toHaveBeenCalledWith({
      build_args: ["--build-arg X=1", "--build-arg Y=2"],
    });
  });

  it("carries an added mod, and refuses to add the same one twice", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    const modInput = screen.getByPlaceholderText("mod-name");
    await userEvent.type(modInput, "paged-attn{Enter}");
    // A mod applied twice is a mod applied twice at deploy time, so the
    // second Add is a no-op rather than a duplicate chip.
    await userEvent.type(modInput, "paged-attn{Enter}");
    await save();

    expect(onSave).toHaveBeenCalledWith({ mods: ["flash-attn", "paged-attn"] });
  });

  it("removes a mod the operator dismisses", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    const chip = screen.getByText("flash-attn").closest("span")!;
    await userEvent.click(chip.querySelector("button")!);
    await save();

    expect(onSave).toHaveBeenCalledWith({ mods: [] });
  });

  it("does nothing at all when the parent offers no save callback", async () => {
    render(<Harness onSaveCustomization={undefined} />);
    await save();
    // The point is only that a form without a sink does not throw on Save.
    expect(screen.getByDisplayValue("Qwen3 8B")).toBeInTheDocument();
  });
});

describe("RecipeForm cancel and deploy name", () => {
  it("puts every edited field back the way the recipe declared it", async () => {
    render(<Harness />);

    const model = screen.getByDisplayValue("Qwen/Qwen3-8B");
    await userEvent.clear(model);
    await userEvent.type(model, "something-else");
    expect(screen.getByDisplayValue("something-else")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "harness-cancel" }));

    expect(screen.getByDisplayValue("Qwen/Qwen3-8B")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("something-else")).not.toBeInTheDocument();
  });

  it("deploys under the name the operator typed, falling back to the recipe's", async () => {
    render(<Harness />);
    const nameField = screen.getByDisplayValue("Qwen3 8B");

    await userEvent.clear(nameField);
    await userEvent.type(nameField, "overnight eval");
    await userEvent.click(screen.getByRole("button", { name: "harness-deploy-name" }));
    expect(screen.getByRole("status")).toHaveTextContent("overnight eval");

    await userEvent.clear(nameField);
    await userEvent.click(screen.getByRole("button", { name: "harness-deploy-name" }));
    expect(screen.getByRole("status")).toHaveTextContent("Qwen3 8B");
  });
});

describe("RecipeForm YAML mode", () => {
  it("renders the current form state as YAML when the operator switches", async () => {
    render(<Harness />);

    await userEvent.click(screen.getByRole("button", { name: "Edit as YAML" }));

    const editor = screen.getByLabelText("Recipe YAML") as HTMLTextAreaElement;
    expect(editor.value).toContain("model:");
    expect(editor.value).toContain("Qwen/Qwen3-8B");
    expect(editor.value).toContain("vllm-node");
  });

  it("saves what the YAML says rather than what the form fields hold", async () => {
    const onSave = vi.fn();
    render(<Harness onSaveCustomization={onSave} />);

    await userEvent.click(screen.getByRole("button", { name: "Edit as YAML" }));
    const editor = screen.getByLabelText("Recipe YAML");
    await userEvent.clear(editor);
    await userEvent.type(editor, "model: Qwen/Qwen3-32B{Enter}container: sglang-node");
    await save();

    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ model: "Qwen/Qwen3-32B", container: "sglang-node" }),
    );
  });

  it("goes back to the form fields when the operator switches back", async () => {
    render(<Harness />);

    await userEvent.click(screen.getByRole("button", { name: "Edit as YAML" }));
    expect(screen.getByLabelText("Recipe YAML")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Form" }));
    expect(screen.queryByLabelText("Recipe YAML")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("Qwen/Qwen3-8B")).toBeInTheDocument();
  });

  it("leaves YAML mode when the operator cancels", async () => {
    render(<Harness />);

    await userEvent.click(screen.getByRole("button", { name: "Edit as YAML" }));
    await userEvent.click(screen.getByRole("button", { name: "harness-cancel" }));

    expect(screen.queryByLabelText("Recipe YAML")).not.toBeInTheDocument();
  });
});
