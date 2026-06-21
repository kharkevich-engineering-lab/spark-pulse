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
});
