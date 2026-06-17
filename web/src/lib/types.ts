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
  is_customized: boolean;
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
  memory_supported: boolean;
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
  is_tracked?: boolean;
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

export interface SecretsResponse {
  hf_token: string; // masked, e.g. "••••••••abc1" or "" if not set
}

export interface ModSummary {
  id: string;
  description: string;
  files: { name: string; kind: string }[];
  has_patches: boolean;
}

export interface ModDetail extends ModSummary {
  script: string;
}

export interface RecipeCustomization {
  command?: string;
  defaults?: Record<string, unknown>;
  env?: Record<string, string>;
  build_args?: string[];
  container?: string;
  model?: string;
  mods?: string[];
}

export interface CustomRecipeInfo {
  id: string;
  name: string;
  filename: string;
  filepath: string;
  created_at: number;
}

export interface CustomModInfo {
  id: string;
  name: string;
  description: string;
  filepath: string;
  has_run_sh: boolean;
}

export interface ModFileMap {
  [filepath: string]: string;
}

export interface RecipeFormRef {
  save: () => void;
  cancel: () => void;
  getDeployName: () => string;
}

export interface GitUpdateStatus {
  git_available: boolean;
  is_repo: boolean;
  local_version: string | null;
  version_available: boolean;
  has_uncommitted_changes: boolean;
  remote_version?: string | null;
  local_date?: string | null;
  remote_date?: string | null;
}

export interface GitUpdateSettings {
  git_update_enabled: boolean;
  git_update_check_interval_seconds: number;
  git_update_auto_pull: boolean;
}

export interface Settings {
  spark_vllm_path: string;
  default_container: string;
  default_gpu_mem_util: number;
  default_port_range_start: number;
  default_port_range_end: number;
  webui_port: number;
  cluster_enabled: boolean;
  job_retention_days: number;
  git_update_enabled: boolean;
  git_update_check_interval_seconds: number;
  git_update_auto_pull: boolean;
  benchmarking_enabled: boolean;
  env_managed?: string[];
}

export interface GitUpdateAction {
  success: boolean;
  error?: string;
}

export interface GitUpdateCheckResult {
  available: boolean;
  local_version: string | null;
  remote_version: string | null;
  local_date: string | null;
  remote_date: string | null;
  has_uncommitted_changes: boolean;
  last_fetch_ok?: boolean;
}

export interface BenchmarkResult {
  benchmark_id: string;
  deployment_id: string;
  recipe_id: string;
  recipe_name: string;
  baseline_id: string | null;
  status: "running" | "completed" | "error";
  started_at: string;
  completed_at: string | null;
  params: Record<string, unknown>;
  results: Record<string, unknown> | null;
}

// ── OCI Registry Types ───────────────────────────────────────────────────────

export interface OciRegistry {
  name: string;
  url: string;
  enabled: boolean;
  default: boolean;
  auth_type: "token" | "username_password" | "none";
  connected?: boolean;
  error?: string;
}

export interface OciCollection {
  name: string;
  version: string;
  display_version: string;
  description: string;
  vendor: string;
  license: string;
  recipe_count: number;
  digest: string;
  registry: string;
}

export interface OciCollectionRecipe {
  name: string;
  description: string;
  model: string;
  container: string;
  recipe_version: string;
  solo_only: boolean;
  cluster_only: boolean;
}

export interface OciRecipeMeta {
  name: string;
  source: string;       // registry name
  collection: string;
  version: string;
  digest: string;
  installed_at: string;
  updated_at: string;
  local_changes: boolean;
}

export interface OciUpdateCheck {
  collection: string;
  current_version: string;
  latest_version: string;
  current_digest: string;
  latest_digest: string;
  local_changes: boolean;
  added_recipes: string[];
  modified_recipes: string[];
}

export interface OciUpdateApply {
  collection: string;
  target_version: string;
  registry: string;
}

export interface OciUpdateResult {
  collection: string;
  success: boolean;
  installed: string[];
  error?: string;
}

export interface OciAutoUpdateSettings {
  enabled: boolean;
  schedule: string;
  overwrite_local: boolean;
}
