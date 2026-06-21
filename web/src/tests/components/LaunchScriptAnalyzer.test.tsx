import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import LaunchScriptAnalyzer from "@/components/LaunchScriptAnalyzer";
import {
  resolveLaunchScript,
  analyzeLaunchScript,
  validateLaunchScript,
} from "@/lib/api";

// Mock API functions
vi.mock("@/lib/api", () => ({
  resolveLaunchScript: vi.fn(),
  analyzeLaunchScript: vi.fn(),
  validateLaunchScript: vi.fn(),
}));

const mockResolve = vi.mocked(resolveLaunchScript);
const mockAnalyze = vi.mocked(analyzeLaunchScript);
const mockValidate = vi.mocked(validateLaunchScript);

describe("LaunchScriptAnalyzer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the component with input and resolve button", () => {
    render(<LaunchScriptAnalyzer />);
    expect(screen.getByPlaceholderText(/path to launch script/i)).toBeInTheDocument();
    expect(screen.getByText("Resolve")).toBeInTheDocument();
    expect(screen.getByText("Launch Script Analysis")).toBeInTheDocument();
  });

  it("shows resolve result when script exists", async () => {
    mockResolve.mockResolvedValue({
      path: "/opt/test/launch.sh",
      exists: true,
      is_file: true,
    });

    render(<LaunchScriptAnalyzer />);
    const input = screen.getByPlaceholderText(/path to launch script/i);
    fireEvent.change(input, { target: { value: "/opt/test/launch.sh" } });
    fireEvent.click(screen.getByText("Resolve"));

    await waitFor(() => {
      expect(mockResolve).toHaveBeenCalledWith({ path: "/opt/test/launch.sh" });
    });

    await waitFor(() => {
      expect(screen.getByText("/opt/test/launch.sh")).toBeInTheDocument();
    });
  });

  it("shows analysis results when script is analyzed", async () => {
    mockResolve.mockResolvedValue({
      path: "/opt/test/launch.sh",
      exists: true,
      is_file: true,
    });
    mockAnalyze.mockResolvedValue({
      path: "/opt/test/launch.sh",
      command_line: "vllm --model test",
      parallelism: { tp: 2, pp: 1, dp: 1 },
      backend: "vllm",
      has_model_flag: true,
      is_valid: true,
      validation: null,
    });
    mockValidate.mockResolvedValue({
      healthy: true,
      errors: [],
      warnings: [],
    });

    render(<LaunchScriptAnalyzer />);
    const input = screen.getByPlaceholderText(/path to launch script/i);
    fireEvent.change(input, { target: { value: "/opt/test/launch.sh" } });
    fireEvent.click(screen.getByText("Resolve"));

    await waitFor(() => {
      expect(screen.getByText("Analyze Script")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Analyze Script"));

    await waitFor(() => {
      expect(mockAnalyze).toHaveBeenCalledWith({ path: "/opt/test/launch.sh" });
    });

    await waitFor(() => {
      expect(screen.getByText("vllm --model test")).toBeInTheDocument();
      expect(screen.getByText("TP")).toBeInTheDocument();
      expect(screen.getByText("2")).toBeInTheDocument();
    });
  });

  it("calls onAnalysisComplete callback when analysis finishes", async () => {
    const onAnalysisComplete = vi.fn();
    mockResolve.mockResolvedValue({
      path: "/opt/test/launch.sh",
      exists: true,
      is_file: true,
    });
    mockAnalyze.mockResolvedValue({
      path: "/opt/test/launch.sh",
      command_line: "vllm --model test",
      parallelism: { tp: 2, pp: 1, dp: 1 },
      backend: "vllm",
      has_model_flag: true,
      is_valid: true,
      validation: null,
    });
    mockValidate.mockResolvedValue({
      healthy: true,
      errors: [],
      warnings: [],
    });

    render(<LaunchScriptAnalyzer onAnalysisComplete={onAnalysisComplete} />);
    const input = screen.getByPlaceholderText(/path to launch script/i);
    fireEvent.change(input, { target: { value: "/opt/test/launch.sh" } });
    fireEvent.click(screen.getByText("Resolve"));
    await waitFor(() => screen.getByText("Analyze Script"));
    fireEvent.click(screen.getByText("Analyze Script"));

    await waitFor(() => {
      expect(onAnalysisComplete).toHaveBeenCalledTimes(1);
    });
  });

  it("disables resolve button when input is empty", () => {
    render(<LaunchScriptAnalyzer />);
    expect(screen.getByText("Resolve")).toBeDisabled();
  });

  it("shows error message when API call fails", async () => {
    mockResolve.mockRejectedValue(new Error("Network error"));

    render(<LaunchScriptAnalyzer />);
    const input = screen.getByPlaceholderText(/path to launch script/i);
    fireEvent.change(input, { target: { value: "/opt/test/launch.sh" } });
    fireEvent.click(screen.getByText("Resolve"));

    await waitFor(() => {
      expect(screen.getByText("Network error")).toBeInTheDocument();
    });
  });
});
