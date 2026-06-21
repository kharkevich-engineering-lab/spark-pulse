import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import ClusterCapacityPanel from "@/components/ClusterCapacityPanel";

const mockCapacity = {
  cluster_name: "test-cluster",
  total_gpus: 2,
  allocated_gpus: 1,
  free_gpus: 1,
  utilization_percent: 50,
  nodes: [
    {
      node_ip: "10.0.0.1",
      role: "head" as const,
      total_gpus: 1,
      allocated_gpus: 1,
      free_gpus: 0,
      total_ram_gb: 96,
      allocated_ram_gb: 48,
      total_cpu_cores: 16,
      allocated_cpu_cores: 8,
      active_deployments: ["deployment-1"],
    },
    {
      node_ip: "10.0.0.2",
      role: "worker" as const,
      total_gpus: 1,
      allocated_gpus: 0,
      free_gpus: 1,
      total_ram_gb: 96,
      allocated_ram_gb: 16,
      total_cpu_cores: 16,
      allocated_cpu_cores: 4,
      active_deployments: [],
    },
  ],
};

describe("ClusterCapacityPanel", () => {
  it("renders the component with header", () => {
    render(<ClusterCapacityPanel capacity={mockCapacity} />);
    expect(screen.getByText("Cluster Capacity")).toBeInTheDocument();
  });

  it("shows node information", () => {
    render(<ClusterCapacityPanel capacity={mockCapacity} />);
    expect(screen.getByText("10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("10.0.0.2")).toBeInTheDocument();
  });

  it("shows allocation bars", () => {
    render(<ClusterCapacityPanel capacity={mockCapacity} />);
    // GPU bar for head node (100% - should be critical)
    expect(screen.getByText("1/1 GPU (100%)")).toBeInTheDocument();
    // RAM bar
    expect(screen.getByText("48/96 GB (50%)")).toBeInTheDocument();
  });

  it("shows active deployments", () => {
    render(<ClusterCapacityPanel capacity={mockCapacity} />);
    expect(screen.getByText("deployment-1")).toBeInTheDocument();
  });

  it("shows warning when allocation is high", () => {
    const highUsageCapacity = {
      ...mockCapacity,
      nodes: [
        {
          node_ip: "10.0.0.1",
          role: "head" as const,
          total_gpus: 1,
          allocated_gpus: 1,
          free_gpus: 0,
          total_ram_gb: 96,
          allocated_ram_gb: 92,
          total_cpu_cores: 16,
          allocated_cpu_cores: 15,
          active_deployments: [],
        },
      ],
    };
    render(<ClusterCapacityPanel capacity={highUsageCapacity} />);
    // Should show warning colors (96% RAM)
    expect(screen.getByText("92/96 GB (96%)")).toBeInTheDocument();
  });

  it("applies custom className", () => {
    const { container } = render(
      <ClusterCapacityPanel capacity={mockCapacity} className="custom-class" />
    );
    expect(container.firstChild).toHaveClass("custom-class");
  });
});
