import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ModValidationDisplay from "@/components/ModValidationDisplay";
import { validateMod, applyMod } from "@/lib/api";

// Mock API functions
vi.mock("@/lib/api", () => ({
  validateMod: vi.fn(),
  applyMod: vi.fn(),
}));

const mockValidate = vi.mocked(validateMod);
const mockApply = vi.mocked(applyMod);

describe("ModValidationDisplay", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the component with validate button", () => {
    render(<ModValidationDisplay modId="test-mod" />);
    expect(screen.getByText("Mod Security Validation")).toBeInTheDocument();
    expect(screen.getByText("Validate")).toBeInTheDocument();
  });

  it("shows validation results when valid", async () => {
    mockValidate.mockResolvedValue({
      healthy: true,
      errors: [],
      warnings: [],
    });

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(mockValidate).toHaveBeenCalledWith({ path: "test-mod" });
    });

    await waitFor(() => {
      expect(screen.getByText("Validation Passed")).toBeInTheDocument();
    });
  });

  it("shows validation errors when present", async () => {
    mockValidate.mockResolvedValue({
      healthy: false,
      errors: ["Unauthorized command detected", "File too large"],
      warnings: [],
    });

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(screen.getByText("Validation Failed")).toBeInTheDocument();
    }, { timeout: 3000 });
  });

  it("shows validation warnings when present", async () => {
    mockValidate.mockResolvedValue({
      healthy: false,
      errors: [],
      warnings: ["Consider adding timeout configuration"],
    });

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(screen.getByText("Warnings Found")).toBeInTheDocument();
    }, { timeout: 3000 });
  });

  it("hides apply button when showApplyButton is false", async () => {
    mockValidate.mockResolvedValue({
      healthy: true,
      errors: [],
      warnings: [],
    });

    render(<ModValidationDisplay modId="test-mod" showApplyButton={false} />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(screen.getByText("Validation Passed")).toBeInTheDocument();
    });

    // Apply button and target selector should not be present
    expect(screen.queryByText(/Apply To/i)).not.toBeInTheDocument();
  });

  it("calls applyMod when apply button is clicked", async () => {
    mockValidate.mockResolvedValue({
      healthy: true,
      errors: [],
      warnings: [],
    });
    mockApply.mockResolvedValue({ success: true } as any);

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(screen.getByText("Validation Passed")).toBeInTheDocument();
    }, { timeout: 3000 });

    // Click "All Nodes" button
    fireEvent.click(screen.getByText("All Nodes"));
    // Click Apply button (text is "Apply Mod to all nodes")
    const applyButton = screen.getByText(/Apply Mod to/i);
    fireEvent.click(applyButton);

    await waitFor(() => {
      expect(mockApply).toHaveBeenCalledWith({
        mod_name: "test-mod",
        mod_path: "test-mod",
        target: "all",
      });
    }, { timeout: 3000 });
  });

  it("shows error when validation fails", async () => {
    mockValidate.mockRejectedValue(new Error("API error"));

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(screen.getByText("API error")).toBeInTheDocument();
    });
  });

  it("calls onValidate callback with result", async () => {
    const onValidate = vi.fn();
    mockValidate.mockResolvedValue({
      healthy: true,
      errors: [],
      warnings: [],
    });

    render(<ModValidationDisplay modId="test-mod" onValidate={onValidate} />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => {
      expect(onValidate).toHaveBeenCalledWith({
        healthy: true,
        errors: [],
        warnings: [],
      });
    });
  });

  /** A mod that validates with warnings is still appliable — the warnings are
   *  what the operator reads before deciding, so they have to be shown next
   *  to the button rather than instead of it. */
  it("shows non-blocking warnings alongside the apply button", async () => {
    mockValidate.mockResolvedValue({
      healthy: true,
      errors: [],
      warnings: ["writes outside /opt", "installs a pip package at start-up"],
    });

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() =>
      expect(screen.getByText("Warnings (non-blocking):")).toBeInTheDocument(),
    );
    // Once, under one heading. A mod that passed used to have every warning
    // printed twice, under "Security Warnings" and again as non-blocking.
    expect(screen.getAllByText("• writes outside /opt")).toHaveLength(1);
    expect(screen.queryByText("Security Warnings:")).toBeNull();
    expect(screen.getByText(/Apply Mod to/)).toBeInTheDocument();
  });

  it("calls a failed mod's warnings security warnings", async () => {
    mockValidate.mockResolvedValue({
      healthy: false,
      errors: ["curl | sh at install time"],
      warnings: ["writes outside /opt"],
    });

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => expect(screen.getByText("Validation Failed")).toBeInTheDocument());
    expect(screen.getByText("Security Warnings:")).toBeInTheDocument();
    expect(screen.getAllByText("• writes outside /opt")).toHaveLength(1);
    // Nothing gets applied off a failed validation.
    expect(screen.queryByText(/Apply Mod to/)).toBeNull();
  });

  /** Which machines a mod lands on is a choice with consequences, so the
   *  button says what it is about to do rather than just "Apply". */
  it("applies to the target the operator picked, and says which", async () => {
    mockValidate.mockResolvedValue({ healthy: true, errors: [], warnings: [] });
    mockApply.mockResolvedValue({ applied: true } as never);

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));
    await waitFor(() => expect(screen.getByText("Apply Mod to all nodes")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Head"));
    expect(screen.getByText("Apply Mod to head")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Workers"));
    expect(screen.getByText("Apply Mod to workers")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Apply Mod to workers"));
    await waitFor(() =>
      expect(mockApply).toHaveBeenCalledWith({
        mod_name: "test-mod",
        mod_path: "test-mod",
        target: "workers",
      }),
    );
    // A successful apply clears the report: the mod is on the nodes now, and
    // leaving a stale "valid" verdict up invites applying it twice.
    await waitFor(() => expect(screen.queryByText(/Apply Mod to/)).toBeNull());
  });

  it("keeps the report and says why when the apply is refused", async () => {
    mockValidate.mockResolvedValue({ healthy: true, errors: [], warnings: [] });
    mockApply.mockRejectedValue(new Error("API 500: no such container on spark-02"));

    render(<ModValidationDisplay modId="test-mod" />);
    fireEvent.click(screen.getByText("Validate"));
    await waitFor(() => expect(screen.getByText("Apply Mod to all nodes")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Apply Mod to all nodes"));
    await waitFor(() =>
      expect(screen.getByText(/no such container on spark-02/)).toBeInTheDocument(),
    );
    expect(screen.getByText("Apply Mod to all nodes")).toBeInTheDocument();
  });

  it("hides the apply button where applying is not on offer", async () => {
    mockValidate.mockResolvedValue({ healthy: true, errors: [], warnings: [] });

    render(<ModValidationDisplay modId="test-mod" showApplyButton={false} />);
    fireEvent.click(screen.getByText("Validate"));

    await waitFor(() => expect(mockValidate).toHaveBeenCalled());
    expect(screen.queryByText(/Apply Mod to/)).toBeNull();
  });
});
