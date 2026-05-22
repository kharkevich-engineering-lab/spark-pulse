export interface RecipeSummary {
  id: string;
  name: string;
  model: string;
  container: string;
  description: string;
  solo_only: boolean;
  cluster_only: boolean;
  mods: string[];
  defaults: Record<string, unknown>;
}

export interface RecipeDetail extends RecipeSummary {
  command: string;
  env: Record<string, string>;
  build_args: string[];
  recipe_version: string;
}

export interface Deployment {
  id: string;
  recipe_id: string;
  name: string;
  params: Record<string, unknown>;
  nodes: string[] | null;
  status: string;
  pid: number | null;
  port: number | null;
  created_at: string;
  started_at: string | null;
  stopped_at: string | null;
  error_message: string | null;
  launch_command?: string;
}

export interface GPUStats {
  index: number;
  gpu: string;
  uuid: string;
  name: string;
  memory_total: number;
  memory_used: number;
  memory_free: number;
  temperature: number | null;
  utilization: number | null;
  power_draw: number | null;
  power_limit: number | null;
}

export interface GPUProcess {
  gpu_uuid: string;
  pid: number;
  process_name: string;
  used_memory: number;
}

export interface CPUStats {
  total: number;
  used: number;
  free: number;
  available: number;
  usage_percent: number;
}

export interface DiskStats {
  mount: string;
  total: number;
  used: number;
  free: number;
  usage_percent: number;
}

export interface MemoryResponse {
  gpu: GPUStats[];
  cpu: CPUStats;
  disk: DiskStats[];
  processes: GPUProcess[];
}

export interface CacheEntry {
  name: string;
  path: string;
  size_bytes: number;
  file_count: number;
  description: string;
  size_human?: string;
}

export interface Settings {
  spark_vllm_path: string;
  default_container: string;
  default_gpu_mem_util: number;
  default_port_range_start: number;
  default_port_range_end: number;
  webui_port: number;
}
