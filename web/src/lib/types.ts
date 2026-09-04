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
  /** "1" (upstream format) or "2" (structured, multi-engine). */
  recipe_version: string;
  /** Preferred engine, or null when the recipe does not name one. */
  engine: string | null;
  /** Engine names this recipe can run on. v1 recipes are vLLM-only. */
  engines: string[];
  /** Engine-neutral parameters. Mirrors `defaults` for v1 recipes. */
  params: Record<string, unknown>;
  /** Where the recipe came from: bundled, upstream, custom, oci or imported. */
  source: string;
  /** Whether each known engine can run this recipe, and why not when it cannot. */
  engine_support: RecipeEngineSupport[];
}

/** One engine's verdict on a recipe, as the backend's engine plugin reports it. */
export interface RecipeEngineSupport {
  engine: string;
  supported: boolean;
  /** Empty when supported; otherwise the plan-time refusal, verbatim. */
  reason: string;
  enabled: boolean;
}

/** Per-engine overrides from a v2 recipe, kept whole after flattening. */
export interface RecipeEngineSpec {
  image: string | null;
  mods: string[];
  env: Record<string, string>;
  args: string;
  command: string | null;
}

export interface RecipeDetail extends RecipeSummary {
  command: string;
  env: Record<string, string>;
  build_args: string[];
  min_nodes: number | null;
  engine_specs: Record<string, RecipeEngineSpec>;
}

// ── Importing recipes from an upstream checkout ─────────────────────────────

export type RecipeImportStatusKind = "ok" | "skipped" | "error";

export interface RecipeImportRecipeEntry {
  file: string;
  id: string | null;
  status: RecipeImportStatusKind;
  message: string;
  name?: string;
  recipe_version?: string;
}

export interface RecipeImportModEntry {
  name: string;
  status: RecipeImportStatusKind;
  message: string;
}

export interface RecipeImportCounts {
  ok: number;
  skipped: number;
  error: number;
}

export interface RecipeImportResult {
  source: string;
  source_url: string | null;
  ref: string | null;
  git_sha: string | null;
  imported_at: string;
  dest: string;
  recipes: RecipeImportRecipeEntry[];
  mods: RecipeImportModEntry[];
  counts: { recipes: RecipeImportCounts; mods: RecipeImportCounts };
}

export type RecipeImportStatus =
  | { imported: false }
  | ({ imported: true } & RecipeImportResult);

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
  /** "native" when the deployment runs as a container we drive ourselves;
   *  absent or "upstream" when it was launched via run-recipe.sh. */
  runtime?: string;
  engine?: string;
  variant?: string;
  image_ref?: string;
  model?: string;
  container_name?: string;
  node_count?: number;
  mods?: string[];
  ready?: boolean;
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

export interface Settings {
  spark_vllm_path: string;
  default_container: string;
  default_gpu_mem_util: number;
  default_port_range_start: number;
  default_port_range_end: number;
  webui_port: number;
  cluster_enabled: boolean;
  cluster_experimental: boolean;
  job_retention_days: number;
  benchmarking_enabled: boolean;
  runtime?: string;
  deploy_ready_timeout_seconds?: number;
  default_engine?: string;
  engine_indexes?: string[];
  engine_index_cache_ttl_seconds?: number;
  engines?: Record<string, { enabled?: boolean }>;
  env_managed?: string[];
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

// ── Cluster Orchestration Types ──────────────────────────────────────────────

export interface ClusterNodeInfo {
  ip: string;
  container: string;
  status: "starting" | "running" | "stopped" | "error";
  ray_ready: boolean;
  gpu_count: number;
}

export interface ClusterState {
  name: string;
  head: ClusterNodeInfo;
  workers: ClusterNodeInfo[];
  ray_enabled: boolean;
  ray_ready: boolean;
  total_nodes: number;
  healthy: boolean;
}

export interface ClusterValidationResult {
  healthy: boolean;
  warnings: string[];
  errors: string[];
}

export interface StartClusterRequest {
  name: string;
  image: string;
  head_ip: string;
  worker_ips: string[];
  env: Record<string, string>;
  docker_config: Record<string, unknown>;
  mod_deployments?: { path: string; target: string }[];
  no_ray?: boolean;
}

export interface StopClusterRequest {
  name: string;
}

export interface ClusterStatusRequest {
  name: string;
}

export interface ClusterValidateRequest {
  name: string;
}

export interface ClusterRollbackRequest {
  name: string;
}

// ── Launch Script Types (Phase 4) ────────────────────────────────────────────

export interface LaunchScriptValidation {
  healthy: boolean;
  warnings: string[];
  errors: string[];
}

export interface LaunchScriptInfo {
  path: string;
  command_line: string | null;
  parallelism: { tp: number; pp: number; dp: number };
  backend: string | null;
  has_model_flag: boolean;
  is_valid: boolean;
  validation: LaunchScriptValidation | null;
}

export interface LaunchScriptResolveResult {
  path: string;
  exists: boolean;
  is_file: boolean;
}

export interface PatchedScriptBundle {
  original_script: string;
  total_nodes: number;
  master_addr: string;
  master_port: number;
  scripts: Record<number, string>;
}

export interface LaunchScriptResolveRequest {
  path: string;
}

export interface LaunchScriptAnalyzeRequest {
  path: string;
}

export interface LaunchScriptValidateRequest {
  path: string;
}

export interface LaunchScriptPatchRequest {
  path: string;
  total_nodes: number;
  master_addr?: string;
  master_port?: number;
}

// ── Mod Deployment Types (Phase 4) ───────────────────────────────────────────

export interface ModValidationResult {
  healthy: boolean;
  warnings: string[];
  errors: string[];
}

export interface ModDeploymentResult {
  mod_name: string;
  target: "head" | "workers" | "all";
  completed_nodes: string[];
  failed_nodes: string[];
}

export interface ModRollbackResult {
  rolled_back_nodes: string[];
}

export interface ModValidateRequest {
  path: string;
}

export interface ModApplyRequest {
  mod_name: string;
  mod_path: string;
  target: "head" | "workers" | "all";
  cluster_state?: ClusterState;
}

export interface ModRollbackRequest {
  mod_name: string;
  mod_path: string;
  target: "head" | "workers" | "all";
  completed_nodes: string[];
  cluster_state?: ClusterState;
}

// ── Deployment Summary (Phase 4) ─────────────────────────────────────────────

export interface DeploymentSummary {
  launch_script: string | null;
  parallelism: { tp: number; pp: number; dp: number };
  total_nodes: number;
  total_gpus: number;
  applied_mods: string[];
  ray_enabled: boolean;
  ray_ready: boolean;
}

// ── Engines ─────────────────────────────────────────────────────────────────

export interface EngineCapabilities {
  mods?: boolean;
  pr_mods?: boolean;
  solo?: boolean;
  cluster?: boolean;
  mesh?: boolean;
}

export interface EngineVerification {
  nodes: number;
  model: string;
  date: string;
  tp?: number;
  pp?: number;
  notes?: string;
}

export interface EnginePorts {
  api: number;
  rendezvous?: number | null;
}

export interface EngineSummary {
  engine: string;
  variant: string;
  key: string;
  description: string;
  image: string;
  image_ref: string;
  version: string;
  tag: string;
  digest: string | null;
  legacy_tags: string[];
  capabilities: EngineCapabilities;
  verified: EngineVerification[];
  ports: EnginePorts;
  readiness: string;
  /** Where the served model id is reported; SGLang's differs from readiness. */
  models_endpoint: string | null;
  metrics: string | null;
  source: string;
  enabled: boolean;
}

export interface EngineDetail extends EngineSummary {
  runtime: Record<string, unknown>;
  sources: Record<string, unknown>;
  arch: string[];
  gpu_arch: string[];
}

export interface EngineListResponse {
  default_engine: string;
  engines: EngineSummary[];
}

export interface EngineIndexRefreshResult {
  refreshed: boolean;
  reason?: string;
  engines: number;
  indexes: { ref: string; status: string; engines?: number; error?: string }[];
}

export interface RenderNode {
  host: string;
  ip?: string;
  eth_if?: string;
  ib_if?: string;
}

export interface RenderRequest {
  recipe_id: string;
  engine?: string;
  variant?: string;
  model?: string;
  params?: Record<string, unknown>;
  extra_args?: string[];
  nodes?: (RenderNode | string)[];
  solo?: boolean;
}

export interface LaunchScript {
  node_rank: number;
  host: string;
  command: string;
  env: Record<string, string>;
  script: string;
}

// ── Deployment plan (dry run) ────────────────────────────────────────────────

export interface DeployPlanRequest {
  recipe_id: string;
  name?: string;
  engine?: string;
  variant?: string;
  model?: string;
  params?: Record<string, unknown>;
  extra_args?: string[];
  nodes?: string[];
  allow_missing_model?: boolean;
}

export interface DeployContainerSpec {
  image: string;
  name: string;
  command: string;
  env: Record<string, string>;
  labels: Record<string, string>;
  mounts: Record<string, string>;
  privileged: boolean;
  ipc_host: boolean;
  network_host: boolean;
  shm_size_gb: number;
  devices: string[];
  cap_add: string[];
  ulimits: Record<string, string>;
  memory_limit_gb: number | null;
  pids_limit: number;
  nofile_limit: number;
  port_mappings: string[];
  entrypoint_clear: boolean;
}

export interface DeployPlan {
  deployment_id: string;
  recipe_id: string;
  recipe_name: string;
  name: string;
  engine: string;
  variant: string;
  image_ref: string;
  model: string;
  solo: boolean;
  nodes: string[];
  node_count: number;
  port: number;
  rendezvous_port: number | null;
  readiness_path: string;
  readiness_url: string;
  metrics_path: string | null;
  mods: string[];
  params: Record<string, unknown>;
  extra_args: string[];
  launch_command: string;
  ranks: LaunchScript[];
  container: DeployContainerSpec;
  cache_mounts: string[];
  /** Whether the image is already on this host, from the plan's own check. */
  image_present: boolean;
  /** Size of the local copy when there is one; null when it must be pulled. */
  image_size_bytes: number | null;
  warnings: string[];
  runtime: string;
  created_at: string;
}

export interface RenderResult {
  recipe_id: string;
  engine: string;
  variant: string;
  image_ref: string;
  model: string;
  solo: boolean;
  nodes: string[];
  readiness: string;
  metrics: string | null;
  ports: EnginePorts;
  cache_mounts: string[];
  container: Record<string, unknown>;
  ranks: LaunchScript[];
}

// ── Models ───────────────────────────────────────────────────────────────────

export interface ModelSource {
  name: string;
  type: "hf_hub" | "local_path";
  endpoint?: string;
  token_secret?: string;
  path?: string;
}

export interface ModelConfigSummary {
  architectures: string[];
  model_type: string | null;
  torch_dtype: string | null;
  quantization: string[];
  quantization_method: string | null;
}

export interface ModelRevision {
  revision: string;
  path: string;
  size_bytes: number;
  last_modified: string | null;
  refs: string[];
  config: ModelConfigSummary | null;
}

export interface ModelEntry {
  id: string;
  source: string;
  source_type: "hf_cache" | "local_path";
  path: string;
  repo_path?: string;
  revision: string | null;
  revisions: ModelRevision[];
  size_bytes: number;
  last_modified: string | null;
  config: ModelConfigSummary | null;
  referenced_by: string[];
}

export type DownloadStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface ModelDownloadJob {
  id: string;
  model: string;
  source: string;
  endpoint?: string | null;
  revision: string | null;
  allow_patterns: string[] | null;
  status: DownloadStatus;
  bytes_done: number;
  bytes_total: number;
  current_file: string | null;
  path: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ModelSyncNodeResult {
  node: string;
  ok: boolean;
  error: string | null;
  duration_s: number;
}

export interface ModelSyncResult {
  model: string;
  path: string;
  ok: boolean;
  results: ModelSyncNodeResult[];
}

export interface ModelPresence {
  model: string;
  local: boolean;
  nodes: { node: string; present: boolean; error: string | null }[];
}

export interface ModelDeleteResult {
  deleted: string;
  path: string;
  freed_bytes: number;
}

// ── Engine images ──────────────────────────────────────────────────

export interface ImageEntry {
  ref: string;
  repository: string;
  tag: string;
  tagged_ref: string;
  engine: string;
  variant: string;
  engine_key: string;
  version: string;
  legacy_tags: string[];
  source: string;
  description: string;
  present: boolean;
  image_id: string;
  size_bytes: number;
  created: string | null;
  /** Digest of the copy on this host, from the image's RepoDigests. */
  local_digest: string;
  /** Digest the engine index advertises for this version, when it has one. */
  index_digest: string;
  /** The index advertises a digest the local tag no longer resolves to. */
  digest_drift: boolean;
  update_available: boolean;
}

export type ImagePullStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface ImagePullJob {
  id: string;
  ref: string;
  repository: string;
  tag: string;
  status: ImagePullStatus;
  bytes_done: number;
  bytes_total: number;
  percent: number;
  layers: number;
  current_status: string | null;
  image_id: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ImageSyncNodeResult {
  node: string;
  ok: boolean;
  skipped: boolean;
  error: string | null;
  duration_s: number;
}

export interface ImageSyncResult {
  ref: string;
  image_id: string;
  ok: boolean;
  results: ImageSyncNodeResult[];
}

export interface ImagePresence {
  ref: string;
  local: boolean;
  image_id: string;
  nodes: { node: string; present: boolean; image_id: string; matches: boolean; error: string | null }[];
}

export interface ImageDeleteResult {
  deleted: string;
  image_id: string;
  freed_bytes: number;
}
