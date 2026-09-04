/** LazyCodeEditor: the YAML editor, and the plain textarea that stands in
 * while its chunk is still downloading.
 *
 * The fallback is the whole point of the component. An operator who opens a
 * custom recipe on a slow link must be able to type into it immediately, and
 * what they type has to reach the same `onChange` the real editor uses — a
 * read-only placeholder would silently drop the first edits.
 */

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import LazyCodeEditor from "@/components/LazyCodeEditor";

describe("LazyCodeEditor", () => {
  it("loads the editor and shows the content it was given", async () => {
    render(
      <LazyCodeEditor value="model: Qwen/Qwen3-8B" language="yaml" onChange={vi.fn()} />,
    );

    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("model: Qwen/Qwen3-8B"));
  });

  it("reports edits to the caller", async () => {
    const onChange = vi.fn();
    render(<LazyCodeEditor value="" language="yaml" onChange={onChange} />);

    const editor = await screen.findByRole("textbox");
    fireEvent.change(editor, { target: { value: "tensor_parallel: 2" } });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("stays editable through the placeholder while the chunk is loading", () => {
    const onChange = vi.fn();
    render(
      <LazyCodeEditor
        value="model: x"
        language="yaml"
        onChange={onChange}
        placeholder="recipe yaml"
        padding={12}
        className="h-64"
      />,
    );

    // Rendered synchronously, before the lazy chunk resolves: this is the
    // Suspense fallback, and it has to be a working textarea.
    const fallback = screen.getByRole("textbox");
    expect(fallback).toHaveValue("model: x");
    expect(fallback).toHaveAttribute("placeholder", "recipe yaml");
    expect(fallback).toHaveClass("h-64");
    expect(fallback).toHaveStyle({ padding: "12px" });

    fireEvent.change(fallback, { target: { value: "model: y" } });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("honours a disabled editor in the placeholder too", () => {
    render(<LazyCodeEditor value="" language="yaml" onChange={vi.fn()} disabled />);
    expect(screen.getByRole("textbox")).toBeDisabled();
  });
});
