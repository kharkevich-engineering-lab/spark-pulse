/** The primitives layer.
 *
 * These are the controls every page now draws from, so what is asserted here
 * is the part a page cannot re-decide: a button that is `type="button"` (a
 * submit by accident inside a form is how a dialog reloads the page), a switch
 * that is a real `switch`, a tablist that answers an arrow key, a field whose
 * error is wired to the control rather than announced somewhere else, and an
 * error line that renders nothing at all when there is no error.
 */

import { describe, expect, it, vi } from "vitest";
import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Save, Trash2 } from "lucide-react";
import {
  Button,
  Card,
  Code,
  EmptyState,
  ErrorLine,
  Field,
  IconButton,
  Input,
  NodeState,
  PageHeader,
  ProgressRow,
  Select,
  Spinner,
  Tabs,
  Textarea,
  Toggle,
  progressPercent,
  transferred,
  isActive,
} from "@/ui";

describe("Button", () => {
  /** A bare `<button>` inside a form submits it. Every one of the ~20 sites
   *  this replaces sat in or near a form, and one of them did exactly that. */
  it("is a button, not a submit, unless asked", () => {
    render(<Button>Deploy</Button>);
    expect(screen.getByRole("button", { name: "Deploy" })).toHaveAttribute("type", "button");
  });

  it("submits when the caller says so", () => {
    render(<Button type="submit">Deploy</Button>);
    expect(screen.getByRole("button", { name: "Deploy" })).toHaveAttribute("type", "submit");
  });

  it.each(["primary", "ghost", "danger"] as const)("renders the %s variant", (variant) => {
    render(<Button variant={variant}>Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).toBeInTheDocument();
  });

  it("renders its icon", () => {
    const { container } = render(<Button icon={Save}>Save</Button>);
    expect(container.querySelector("svg")).toBeInTheDocument();
  });

  /** The spinner takes the icon's place rather than sitting beside it, so a
   *  row of buttons does not reflow the moment one is pressed. */
  it("swaps the icon for a spinner while loading, and refuses a second click", async () => {
    const onClick = vi.fn();
    render(
      <Button icon={Save} loading onClick={onClick}>
        Save
      </Button>,
    );

    const button = screen.getByRole("button", { name: /Save/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    await userEvent.click(button, { pointerEventsCheck: 0 });
    expect(onClick).not.toHaveBeenCalled();
  });

  it("forwards a ref", () => {
    const ref = createRef<HTMLButtonElement>();
    render(<Button ref={ref}>Go</Button>);
    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
  });

  it("renders a small button", async () => {
    const onClick = vi.fn();
    render(
      <Button size="sm" onClick={onClick}>
        Go
      </Button>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(onClick).toHaveBeenCalled();
  });
});

describe("IconButton", () => {
  /** An icon button with no accessible name is a button nobody can describe,
   *  which is why the label is required rather than optional. */
  it("takes its accessible name from the label", () => {
    render(<IconButton icon={Trash2} label="Forget spark-02" />);
    expect(screen.getByRole("button", { name: "Forget spark-02" })).toHaveAttribute(
      "title",
      "Forget spark-02",
    );
  });

  it("shows a spinner instead of the icon while loading", () => {
    render(<IconButton icon={Trash2} label="Forget" loading />);
    expect(screen.getByRole("button", { name: "Forget" })).toBeDisabled();
  });

  it("renders at the small size", () => {
    render(<IconButton size="sm" icon={Trash2} label="Forget" />);
    expect(screen.getByRole("button", { name: "Forget" })).toHaveClass("w-8");
  });

  it("forwards a ref", () => {
    const ref = createRef<HTMLButtonElement>();
    render(<IconButton ref={ref} icon={Trash2} label="Forget" />);
    expect(ref.current).toBeInstanceOf(HTMLButtonElement);
  });
});

describe("Field", () => {
  it("points its label at the control and reads back what was typed", async () => {
    render(
      <Field label="Address">
        {(control) => <Input {...control} defaultValue="" placeholder="10.0.0.11" />}
      </Field>,
    );

    await userEvent.type(screen.getByLabelText("Address"), "10.0.0.12");
    expect(screen.getByLabelText("Address")).toHaveValue("10.0.0.12");
  });

  /** The failure is read *with* the field, not announced somewhere else on
   *  the page — which is what a banner at the top does. */
  it("wires an error to the control it belongs to", () => {
    render(
      <Field label="Port" error="already bound">
        {(control) => <Input {...control} />}
      </Field>,
    );

    const input = screen.getByLabelText("Port");
    expect(input).toHaveAttribute("aria-invalid", "true");
    const error = screen.getByRole("alert");
    expect(error).toHaveTextContent("already bound");
    expect(input.getAttribute("aria-describedby")).toContain(error.id);
  });

  it("describes the control with its hint", () => {
    render(
      <Field label="Port" hint="9000 unless a recipe says otherwise">
        {(control) => <Input {...control} />}
      </Field>,
    );

    const input = screen.getByLabelText("Port");
    expect(input.getAttribute("aria-describedby")).toBeTruthy();
    expect(screen.getByText("9000 unless a recipe says otherwise")).toBeInTheDocument();
  });

  it("accepts a plain child rather than a function", () => {
    render(
      <Field label="Notes">
        <Textarea aria-label="Notes" />
      </Field>,
    );
    expect(screen.getByLabelText("Notes")).toBeInTheDocument();
  });

  it("renders a select", async () => {
    render(
      <Field label="Source">
        {(control) => (
          <Select {...control} defaultValue="hf_hub">
            <option value="hf_hub">hf_hub</option>
            <option value="local_path">local_path</option>
          </Select>
        )}
      </Field>,
    );

    await userEvent.selectOptions(screen.getByLabelText("Source"), "local_path");
    expect(screen.getByLabelText("Source")).toHaveValue("local_path");
  });

  it("renders a mono input and a mono textarea", () => {
    const { container } = render(
      <>
        <Input mono aria-label="ref" />
        <Textarea mono aria-label="body" />
      </>,
    );
    expect(container.querySelectorAll(".font-mono")).toHaveLength(2);
  });
});

describe("Toggle", () => {
  it("is a switch with a name, and reports its state", async () => {
    const onChange = vi.fn();
    render(<Toggle on={false} onChange={onChange} label="Custom mode" />);

    const toggle = screen.getByRole("switch", { name: "Custom mode" });
    expect(toggle).toHaveAttribute("aria-checked", "false");

    await userEvent.click(toggle);
    expect(onChange).toHaveBeenCalledWith(true);
  });

  /** Space and Enter come free from being a real button; two of the four
   *  toggles this replaces were `div`s with an `onClick`. */
  it("answers the keyboard", async () => {
    const onChange = vi.fn();
    render(<Toggle on onChange={onChange} label="Custom mode" />);

    screen.getByRole("switch").focus();
    await userEvent.keyboard(" ");
    expect(onChange).toHaveBeenCalledWith(false);
  });

  it("refuses to change while disabled", async () => {
    const onChange = vi.fn();
    render(<Toggle on={false} onChange={onChange} label="Custom mode" disabled />);

    await userEvent.click(screen.getByRole("switch"), { pointerEventsCheck: 0 });
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("Tabs", () => {
  const TABS = [
    { id: "recipes", label: "Recipes", count: 3 },
    { id: "mods", label: "Mods", count: 0 },
    { id: "history", label: "History" },
  ];

  it("marks the selected tab and reports a click", async () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} value="recipes" onChange={onChange} label="Deploy" />);

    expect(screen.getByRole("tab", { name: /Recipes \(3\)/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await userEvent.click(screen.getByRole("tab", { name: /Mods \(0\)/ }));
    expect(onChange).toHaveBeenCalledWith("mods");
  });

  /** `role="tablist"` promises a keyboard user that the arrows move between
   *  tabs. All four bars this replaces declared the role and answered none of
   *  them, which is worse than declaring nothing. */
  it.each([
    ["{ArrowRight}", "mods"],
    ["{ArrowLeft}", "history"],
    ["{End}", "history"],
  ])("moves on %s", async (key, expected) => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} value="recipes" onChange={onChange} />);

    screen.getByRole("tab", { name: /Recipes/ }).focus();
    await userEvent.keyboard(key);
    expect(onChange).toHaveBeenCalledWith(expected);
  });

  it("goes to the first tab on Home", async () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} value="history" onChange={onChange} />);

    screen.getByRole("tab", { name: "History" }).focus();
    await userEvent.keyboard("{Home}");
    expect(onChange).toHaveBeenCalledWith("recipes");
  });

  it("ignores a key that is not navigation", async () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} value="recipes" onChange={onChange} />);

    screen.getByRole("tab", { name: /Recipes/ }).focus();
    await userEvent.keyboard("a");
    expect(onChange).not.toHaveBeenCalled();
  });

  /** A value no tab carries — a page mid-transition — must not move the
   *  selection to whatever happens to be first. */
  it("does nothing when the value names no tab", async () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} value="nothing" onChange={onChange} />);

    screen.getAllByRole("tab")[0].focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("omits the count when a tab has none", () => {
    render(<Tabs tabs={TABS} value="history" onChange={vi.fn()} />);
    expect(screen.getByRole("tab", { name: "History" })).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("says the one line, and the hint when there is one", () => {
    render(
      <EmptyState icon={Save} hint="Recipes come from a collection." action={<Button>Add</Button>}>
        No recipes yet.
      </EmptyState>,
    );

    expect(screen.getByText("No recipes yet.")).toBeInTheDocument();
    expect(screen.getByText("Recipes come from a collection.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeInTheDocument();
  });

  it("is just the line when that is all there is", () => {
    render(<EmptyState>Nothing is running.</EmptyState>);
    expect(screen.getByText("Nothing is running.")).toBeInTheDocument();
  });
});

describe("ErrorLine", () => {
  it("is an alert when there is something to say", () => {
    render(<ErrorLine>port 9000 is already bound</ErrorLine>);
    expect(screen.getByRole("alert")).toHaveTextContent("port 9000 is already bound");
  });

  /** So a caller can hand it `error` directly rather than guarding at every
   *  one of the eight sites this replaces. */
  it.each([[null], [undefined], [""]])("renders nothing for %s", (value) => {
    const { container } = render(<ErrorLine>{value}</ErrorLine>);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("Spinner", () => {
  it("is announced when it is given something to say", () => {
    render(<Spinner label="Loading…" />);
    expect(screen.getByRole("status", { name: "Loading…" })).toBeInTheDocument();
  });

  /** A spinner beside a label that already says "Saving…" is noise. */
  it("is hidden from the accessibility tree when it has no label", () => {
    const { container } = render(<Spinner size="sm" />);
    expect(screen.queryByRole("status")).toBeNull();
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("Code and Card", () => {
  it("renders an inline literal", () => {
    render(<Code>~/.config/spark-pulse/settings.json</Code>);
    expect(screen.getByText("~/.config/spark-pulse/settings.json")).toBeInTheDocument();
  });

  it("renders a panel, and lifts only when it is a target", () => {
    const { container, rerender } = render(<Card>panel</Card>);
    expect(container.firstChild).not.toHaveClass("hover:-translate-y-0.5");

    rerender(
      <Card interactive padding="destination">
        panel
      </Card>,
    );
    expect(container.firstChild).toHaveClass("hover:-translate-y-0.5");
  });
});

describe("PageHeader", () => {
  it("carries the eyebrow, the title, the one-line description and the actions", () => {
    render(
      <PageHeader
        eyebrow="Fleet"
        title="What is serving."
        description="Every run this control plane started."
        actions={<Button>Deploy a recipe</Button>}
      />,
    );

    expect(screen.getByRole("heading", { name: "What is serving." })).toBeInTheDocument();
    expect(screen.getByText("Fleet")).toBeInTheDocument();
    expect(screen.getByText("Every run this control plane started.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Deploy a recipe" })).toBeInTheDocument();
  });

  it("is just a title when that is all it was given", () => {
    render(<PageHeader title="Settings." />);
    expect(screen.getByRole("heading", { name: "Settings." })).toBeInTheDocument();
  });
});

describe("ProgressRow", () => {
  /** The two copies disagreed: one measured bytes, the other read the percent
   *  the backend already sent. Both are right for their job. */
  it.each([
    [{ status: "running", bytes_done: 50, bytes_total: 200 }, 25],
    [{ status: "running", percent: 73.4 }, 73],
    [{ status: "completed", bytes_done: 0, bytes_total: 0 }, 100],
    // No total yet: 0, not a division by zero.
    [{ status: "running", bytes_done: 10, bytes_total: null }, 0],
    // A percent out of range is clamped rather than drawn off the end.
    [{ status: "running", percent: 240 }, 100],
  ])("reads %o as %i%%", (job, expected) => {
    expect(progressPercent(job)).toBe(expected);
  });

  it("says how much of how much, and ? while the total is unknown", () => {
    expect(transferred({ status: "running", bytes_done: 1024, bytes_total: 4096 })).toBe(
      "1.0 KB / 4.0 KB",
    );
    expect(transferred({ status: "running", bytes_done: 1024 })).toBe("1.0 KB / ?");
  });

  it("knows which jobs can still be cancelled", () => {
    expect(isActive({ status: "running" })).toBe(true);
    expect(isActive({ status: "queued" })).toBe(true);
    expect(isActive({ status: "completed" })).toBe(false);
  });

  it("names its bar and offers a cancel only when there is one", async () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <ProgressRow
        title="acme/qwen3-8b"
        detail="running · model.safetensors"
        job={{ status: "running", bytes_done: 50, bytes_total: 200 }}
        progressLabel="acme/qwen3-8b progress"
        cancelLabel="Cancel acme/qwen3-8b"
        onCancel={onCancel}
      />,
    );

    expect(screen.getByRole("progressbar", { name: "acme/qwen3-8b progress" })).toHaveAttribute(
      "aria-valuenow",
      "25",
    );
    await userEvent.click(screen.getByRole("button", { name: "Cancel acme/qwen3-8b" }));
    expect(onCancel).toHaveBeenCalled();

    rerender(
      <ProgressRow
        title="acme/qwen3-8b"
        job={{ status: "completed" }}
        progressLabel="acme/qwen3-8b progress"
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("falls back to a plain cancel label", async () => {
    const onCancel = vi.fn();
    render(
      <ProgressRow
        title="x"
        job={{ status: "running" }}
        progressLabel="x progress"
        onCancel={onCancel}
      >
        <p>a scheduled deploy</p>
      </ProgressRow>,
    );

    expect(screen.getByText("a scheduled deploy")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
  });
});

describe("NodeState", () => {
  it.each([
    ["ok", "Healthy"],
    ["warn", "Degraded"],
    ["bad", "Dead"],
    ["unknown", "Unknown"],
  ] as const)("says %s as %s", (state, label) => {
    render(<NodeState state={state} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  /** A node that could not be reached has not failed, and a page that renders
   *  silence as a failure teaches an operator to ignore it. */
  it("explains unknown on hover, in words", () => {
    render(<NodeState state="unknown" />);
    expect(screen.getByTestId("node-state-unknown")).toHaveAttribute(
      "title",
      expect.stringContaining("unverified"),
    );
  });

  it("takes the caller's own word and reason", () => {
    render(<NodeState state="warn" label="Different image" title="digest drift" />);
    expect(screen.getByText("Different image")).toHaveAttribute("title", "digest drift");
  });

  it("keeps the word for a screen reader when only the dot is drawn", () => {
    render(<NodeState state="ok" dotOnly label="Connected" />);
    expect(screen.getByText("Connected")).toHaveClass("sr-only");
  });

  it("falls back to unknown for a state it has never heard of", () => {
    // A newer backend word must not render as a blank chip.
    render(<NodeState state={"quantum" as never} />);
    expect(screen.getByText("Unknown")).toBeInTheDocument();
  });
});
