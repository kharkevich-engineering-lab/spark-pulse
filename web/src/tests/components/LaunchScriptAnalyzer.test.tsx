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

  /** A path that resolves to nothing is the ordinary mistake — a typo, or a
   *  checkout that is not where it was last time — so it has to name the
   *  path it looked for rather than reporting a generic failure. */
  it("names the path when the script is not there", async () => {
    mockResolve.mockResolvedValue({
      path: "/opt/test/missing.sh",
      exists: false,
      is_file: false,
    });

    render(<LaunchScriptAnalyzer />);
    fireEvent.change(screen.getByPlaceholderText(/path to launch script/i), {
      target: { value: "/opt/test/missing.sh" },
    });
    fireEvent.click(screen.getByText("Resolve"));

    await waitFor(() => {
      expect(screen.getByText("Script not found: /opt/test/missing.sh")).toBeInTheDocument();
    });
  });

  /** Validation is the point of the panel, and its three outcomes are three
   *  different answers: passed, warnings worth reading, and errors that stop
   *  the script. Each one has to be legible as itself. */
  it("separates a failed validation's errors from its warnings", async () => {
    mockResolve.mockResolvedValue({ path: "/opt/test/launch.sh", exists: true, is_file: true });
    mockAnalyze.mockResolvedValue({
      path: "/opt/test/launch.sh",
      command_line: "vllm serve --tensor-parallel-size 4",
      parallelism: { tp: 4, pp: 1, dp: 1 },
      backend: "vllm",
      has_model_flag: false,
      validation: null,
    } as never);
    mockValidate.mockResolvedValue({
      valid: false,
      errors: ["--model is required"],
      warnings: ["tp=4 exceeds the GPUs on this node"],
    } as never);

    render(<LaunchScriptAnalyzer />);
    fireEvent.change(screen.getByPlaceholderText(/path to launch script/i), {
      target: { value: "/opt/test/launch.sh" },
    });
    fireEvent.click(screen.getByText("Resolve"));
    await waitFor(() => screen.getByText("Analyze Script"));
    fireEvent.click(screen.getByText("Analyze Script"));

    await waitFor(() => expect(screen.getByText("Validation Failed")).toBeInTheDocument());
    expect(screen.getByText("• --model is required")).toBeInTheDocument();
    expect(screen.getByText("• tp=4 exceeds the GPUs on this node")).toBeInTheDocument();
    // The analysis above it stays readable: a missing flag is called out.
    expect(screen.getByText("Missing --model flag")).toBeInTheDocument();
  });

  it("calls warnings warnings, not failures", async () => {
    mockResolve.mockResolvedValue({ path: "/opt/test/launch.sh", exists: true, is_file: true });
    mockAnalyze.mockResolvedValue({
      path: "/opt/test/launch.sh",
      command_line: "",
      parallelism: { tp: 1, pp: 1, dp: 1 },
      backend: "",
      has_model_flag: true,
      validation: null,
    } as never);
    mockValidate.mockResolvedValue({
      valid: false,
      errors: [],
      warnings: ["no --port given; the engine default will be used"],
    } as never);

    render(<LaunchScriptAnalyzer />);
    fireEvent.change(screen.getByPlaceholderText(/path to launch script/i), {
      target: { value: "/opt/test/launch.sh" },
    });
    fireEvent.click(screen.getByText("Resolve"));
    await waitFor(() => screen.getByText("Analyze Script"));
    fireEvent.click(screen.getByText("Analyze Script"));

    await waitFor(() => expect(screen.getByText("Validation Warnings")).toBeInTheDocument());
    expect(screen.queryByText("Errors:")).toBeNull();
  });
});
