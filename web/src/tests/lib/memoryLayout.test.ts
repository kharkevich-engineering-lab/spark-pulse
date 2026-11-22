/** How a node's unified memory is cut by run.
 *
 * The figures come from the node alone: processes that name a run are that
 * run, the rest of the GPU processes are "other", and what remains of `used`
 * is the system. Nothing here may invent a run from a process name.
 */

import { describe, expect, it } from "vitest";
import { formatMb, memoryLayout, runColors } from "@/lib/memoryLayout";
import type { NodeStats } from "@/lib/types";

function stats(over: Partial<NodeStats> = {}): NodeStats {
  return {
    id: "n1",
    name: "spark-01",
    address: "10.0.0.1",
    is_control_plane: true,
    reachable: true,
    error: null,
    unavailable: [],
    gpu: [],
    cpu: { total: 120 * 1024, used: 40 * 1024, free: 80 * 1024, available: 80 * 1024, usage_percent: 33 },
    disk: [],
    processes: [],
    ...over,
  };
}

const runs = [
  { id: "a", name: "qwen" },
  { id: "b", name: "bonsai" },
];

describe("runColors", () => {
  it("gives each run its own slot, wrapping after five", () => {
    const colors = runColors([{ id: "1" }, { id: "2" }, { id: "3" }, { id: "4" }, { id: "5" }, { id: "6" }]);
    expect(colors.get("1")).toBe("var(--run-1)");
    expect(colors.get("2")).toBe("var(--run-2)");
    expect(colors.get("6")).toBe("var(--run-1)");
  });
});

describe("memoryLayout", () => {
  it("is null for a node that did not answer or reported no total", () => {
    const colors = runColors(runs);
    expect(memoryLayout(stats({ reachable: false }), runs, colors)).toBeNull();
    expect(
      memoryLayout(stats({ cpu: { total: 0, used: 0, free: 0, available: 0, usage_percent: 0 } }), runs, colors),
    ).toBeNull();
  });

  it("cuts used memory into runs, other processes and the system", () => {
    const layout = memoryLayout(
      stats({
        processes: [
          { gpu_uuid: "g", pid: 1, process_name: "vllm", used_memory: 20 * 1024, deployment: "a" },
          { gpu_uuid: "g", pid: 2, process_name: "vllm", used_memory: 5 * 1024, deployment: "a" },
          { gpu_uuid: "g", pid: 3, process_name: "llama", used_memory: 10 * 1024, deployment: "b" },
          { gpu_uuid: "g", pid: 4, process_name: "python3", used_memory: 1024 },
          { gpu_uuid: "g", pid: 5, process_name: "idle", used_memory: 0 },
        ],
      }),
      runs,
      runColors(runs),
    )!;

    expect(layout.totalMb).toBe(120 * 1024);
    expect(layout.usedMb).toBe(40 * 1024);
    expect(layout.freeMb).toBe(80 * 1024);
    expect(layout.segments.map((s) => [s.kind, s.label, s.mb])).toEqual([
      ["system", "", 4 * 1024],
      ["run", "qwen", 25 * 1024],
      ["run", "bonsai", 10 * 1024],
      ["other", "", 1024],
    ]);
    expect(layout.segments[1].color).toBe("var(--run-1)");
    expect(layout.segments[2].color).toBe("var(--run-2)");
  });

  it("trusts the processes when they hold more than `used` says, capped at the total", () => {
    const layout = memoryLayout(
      stats({
        cpu: { total: 100, used: 10, free: 90, available: 90, usage_percent: 10 },
        processes: [{ gpu_uuid: "g", pid: 1, process_name: "x", used_memory: 150, deployment: "zzz-unknown-run" }],
      }),
      runs,
      new Map(),
    )!;
    expect(layout.usedMb).toBe(100);
    expect(layout.freeMb).toBe(0);
    // A run this page does not list still shows, under a short id.
    expect(layout.segments).toEqual([
      { kind: "run", id: "zzz-unknown-run", label: "zzz-unknown-", mb: 150, color: "var(--run-1)" },
    ]);
  });

  it("copes with a node that reports no processes at all", () => {
    const layout = memoryLayout(stats({ processes: undefined as never }), runs, new Map())!;
    expect(layout.segments).toEqual([{ kind: "system", id: "", label: "", mb: 40 * 1024, color: "var(--mem-system)" }]);
  });
});

describe("formatMb", () => {
  it("prints GB with a decimal from one GB up, and whole MB below", () => {
    expect(formatMb(2048)).toBe("2.0 GB");
    expect(formatMb(512.4)).toBe("512 MB");
  });
});
