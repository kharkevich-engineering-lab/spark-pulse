import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/lib/api", () => ({
  planKubernetes: vi.fn(),
}));

import { planKubernetes } from "@/lib/api";
import { KubernetesPreview } from "@/components/KubernetesPreview";
import type { KubernetesPlan } from "@/lib/types";

const BODY = { recipe_id: "bundled/qwen2.5-0.5b-instruct", params: { port: 8000 } };

const PLAN: KubernetesPlan = {
  backend: "kubernetes",
  deployment_id: "abc",
  recipe_id: BODY.recipe_id,
  engine: "vllm",
  variant: "default",
  image: "ghcr.io/example/vllm:0.1.0",
  namespace: "spark-pulse",
  port: 8000,
  intent_hash: "f00",
  manifests: [],
  yaml: "apiVersion: apps/v1\nkind: Deployment\n",
  findings: [{ code: "gpus-per-rank", detail: "each pod requests one GPU" }],
  warnings: [],
  applied: false,
};

describe("KubernetesPreview", () => {
  // Braces, not an expression: a function returned from beforeEach is run as
  // the test's teardown, and mockReset() returns the mock itself.
  beforeEach(() => {
    vi.mocked(planKubernetes).mockReset();
  });

  it("asks for nothing until the operator renders", () => {
    render(<KubernetesPreview body={BODY} />);
    expect(planKubernetes).not.toHaveBeenCalled();
    expect(screen.queryByTestId("kubernetes-yaml")).toBeNull();
  });

  it("renders the manifests and what the intent could not say", async () => {
    vi.mocked(planKubernetes).mockResolvedValue(PLAN);
    render(<KubernetesPreview body={BODY} />);

    await userEvent.setup().click(screen.getByTestId("kubernetes-render"));

    expect(await screen.findByTestId("kubernetes-yaml")).toHaveTextContent("kind: Deployment");
    expect(screen.getByTestId("kubernetes-findings")).toHaveTextContent("gpus-per-rank");
    expect(screen.getByText(/Nothing is applied/)).toBeInTheDocument();
    expect(planKubernetes).toHaveBeenCalledWith({ ...BODY, hf_cache: "pvc" });
  });

  it("sends the cache the operator chose", async () => {
    vi.mocked(planKubernetes).mockResolvedValue({ ...PLAN, findings: [] });
    render(<KubernetesPreview body={BODY} />);

    const user = userEvent.setup();
    await user.selectOptions(screen.getByTestId("kubernetes-cache"), "hostPath");
    await user.click(screen.getByTestId("kubernetes-render"));

    await waitFor(() =>
      expect(planKubernetes).toHaveBeenCalledWith({ ...BODY, hf_cache: "hostPath" }),
    );
    expect(await screen.findByTestId("kubernetes-yaml")).toBeInTheDocument();
    expect(screen.queryByTestId("kubernetes-findings")).toBeNull();
  });

  it("says why when the backend refuses", async () => {
    vi.mocked(planKubernetes).mockRejectedValue(new Error("spans nodes over RPC"));
    render(<KubernetesPreview body={BODY} />);

    await userEvent.setup().click(screen.getByTestId("kubernetes-render"));

    expect(await screen.findByRole("alert")).toHaveTextContent("spans nodes over RPC");
    expect(screen.queryByTestId("kubernetes-yaml")).toBeNull();
  });

  it("falls back to its own words for an error that is not one", async () => {
    vi.mocked(planKubernetes).mockRejectedValue("boom");
    render(<KubernetesPreview body={BODY} />);

    await userEvent.setup().click(screen.getByTestId("kubernetes-render"));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not render the manifests");
  });
});
