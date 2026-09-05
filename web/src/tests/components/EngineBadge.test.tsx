/** EngineBadge and EngineList: which engine, which variant, and whether it is
 * switched on.
 *
 * The variant is the load-bearing part. `vllm` and `vllm/b12x` are two
 * different images with different kernels, so a badge that renders both as
 * "vllm" gives an operator two identical-looking rows and no way to tell which
 * one is running.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import EngineBadge, { EngineList } from "@/components/EngineBadge";
import type { EngineSummary } from "@/lib/types";

const engine = (over: Partial<EngineSummary> = {}): EngineSummary => ({
  engine: "vllm",
  variant: "default",
  key: "vllm",
  description: "vLLM",
  image: "ghcr.io/example/vllm",
  image_ref: "ghcr.io/example/vllm:0.1.0",
  version: "0.1.0",
  tag: "0.1.0",
  digest: null,
  legacy_tags: [],
  capabilities: { solo: true, cluster: true, mods: false },
  verified: [],
  ports: { api: 8000, rendezvous: null },
  readiness: "/v1/models",
  models_endpoint: "/v1/models",
  metrics: null,
  source: "bundled",
  enabled: true,
  ...over,
});

describe("EngineBadge", () => {
  it("says just the engine when the variant is the default one", () => {
    render(<EngineBadge engine="vllm" variant="default" />);
    expect(screen.getByText("vllm")).toBeInTheDocument();
  });

  it("names the variant when there is one, so two images do not read alike", () => {
    render(<EngineBadge engine="vllm" variant="b12x" />);
    expect(screen.getByText("vllm · b12x")).toBeInTheDocument();
  });

  it("marks the default engine", () => {
    render(<EngineBadge engine="vllm" isDefault />);
    expect(screen.getByText("default")).toBeInTheDocument();
  });

  /** A disabled engine still appears in lists. It has to look disabled, or an
   *  operator picks one that cannot run. */
  it("marks a disabled engine off", () => {
    render(<EngineBadge engine="sglang" enabled={false} />);
    expect(screen.getByText("off")).toBeInTheDocument();
  });

  it("does not mark an enabled engine off", () => {
    render(<EngineBadge engine="sglang" />);
    expect(screen.queryByText("off")).not.toBeInTheDocument();
  });
});

describe("EngineList", () => {
  it("says so plainly when the registry holds no engines", () => {
    render(<EngineList engines={[]} defaultEngine="vllm" />);
    expect(screen.getByText("No engines available.")).toBeInTheDocument();
  });

  it("lists each engine with its version and capabilities", () => {
    render(
      <EngineList
        engines={[
          engine(),
          engine({
            engine: "sglang",
            key: "sglang",
            version: "0.4.2",
            image: "ghcr.io/example/sglang",
            image_ref: "ghcr.io/example/sglang:0.4.2",
            capabilities: { solo: true, cluster: false },
            ports: { api: 30000, rendezvous: 29500 },
          }),
        ]}
        defaultEngine="vllm"
      />,
    );

    expect(screen.getByText("vllm")).toBeInTheDocument();
    expect(screen.getByText("v0.1.0")).toBeInTheDocument();
    expect(screen.getByText("sglang")).toBeInTheDocument();
    expect(screen.getByText("v0.4.2")).toBeInTheDocument();
    // Only the true capabilities are named.
    expect(screen.getByText(/solo, cluster/)).toBeInTheDocument();
    // A rendezvous port is shown next to the api port, because a multi-node
    // deploy needs both open.
    expect(screen.getByText("/ :29500")).toBeInTheDocument();
  });

  it("marks the engine that recipes fall back to", () => {
    render(<EngineList engines={[engine()]} defaultEngine="vllm" />);
    expect(screen.getByText("default")).toBeInTheDocument();
  });

  /** A variant is never the default, however the engine name matches: the
   *  default is `<engine>/default`. */
  it("does not mark a variant as the default engine", () => {
    render(<EngineList engines={[engine({ variant: "b12x", key: "vllm-b12x" })]} defaultEngine="vllm" />);
    expect(screen.queryByText("default")).not.toBeInTheDocument();
  });

  it("shows a pinned digest instead of the mutable tag", () => {
    render(
      <EngineList
        engines={[engine({ digest: "sha256:abcdef0123456789abcdef" })]}
        defaultEngine="vllm"
      />,
    );
    expect(screen.getByText(/ghcr\.io\/example\/vllm@sha256:abcdef012/)).toBeInTheDocument();
  });

  it("falls back to the image ref when nothing is pinned", () => {
    render(<EngineList engines={[engine()]} defaultEngine="vllm" />);
    expect(screen.getByText("ghcr.io/example/vllm:0.1.0")).toBeInTheDocument();
  });

  it("marks an engine that has actually been verified on hardware", () => {
    render(
      <EngineList
        engines={[engine({ verified: [{ nodes: 1, model: "Qwen3-8B", date: "2026-01-01" }] })]}
        defaultEngine="vllm"
      />,
    );
    expect(screen.getByText("verified")).toBeInTheDocument();
  });

  it("says an engine declares no capabilities rather than leaving a gap", () => {
    render(<EngineList engines={[engine({ capabilities: {} })]} defaultEngine="none" />);
    expect(screen.getByText(/no capabilities declared/)).toBeInTheDocument();
  });
});
