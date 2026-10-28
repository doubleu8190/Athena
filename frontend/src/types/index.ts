import type { ApplicationEventType } from "./events"
import type { ExecutionError } from "./events"
export type { ExecutionError }

// ─── 执行步骤相关 ───────────────────────────────────────────────

export type StepType = "llm_call" | "tool_execution"

export type StepStatus = "pending" | "running" | "paused" | "completed" | "failed"

export interface Step {
  id: string
  session_id: string
  run_id: string
  step_number: number
  step_type: StepType
  parent_step_id?: string | null
  /** 父 run ID（子 Agent 步骤指向其父 run；主 run 步骤为 null） */
  parent_run_id?: string | null
  status: StepStatus
  started_at: string
  completed_at?: string | null
  duration_ms: number
  llm_input_tokens: number
  llm_output_tokens: number
  error_message?: string | null
  error_detail?: ExecutionError | null
}

// ─── 消息相关 ───────────────────────────────────────────────────

export type MessageRole = "user" | "assistant" | "system" | "tool"

export interface ToolCallInvocation {
  id: string
  name: string
  args: Record<string, unknown>
}

export type RetrievalSource = "memory" | "file"
export type RetrievalReferenceStatus = "recorded" | "available_on_feedback"

export interface RetrievalReference {
  event_id: string
  source: RetrievalSource
  status: RetrievalReferenceStatus
  result_count: number
}

export interface RetrievalResult {
  id: string
  source: RetrievalSource
  title?: string | null
  summary?: string | null
  content?: string | null
  locator?: Record<string, unknown> | null
  score?: number | null
}


export interface Message {
  id: string
  role: MessageRole
  content: string
  timestamp: string
  /** Event sequence when available; used only to break equal-time display ties. */
  session_seq?: number
  session_id?: string
  /** 关联的步骤 ID（assistant 指向 llm_call 步骤，tool 指向工具步骤） */
  step_id?: string
  /** 关联的 tool_call 记录 ID（tool 消息） */
  tool_call_record_id?: string
  /** 工具名（tool 消息，工具气泡标名） */
  tool_name?: string
  /** 消息类型标记（如 "conversation_summary"） */
  type?: string
  tool_call_id?: string
  /** 所属运行 ID（一次用户请求 ≈ 一个 run），Activity 面板按此归组 */
  run_id?: string
  stream_id?: string
  stream_status?: "streaming" | "finalizing" | "unconfirmed" | "completed" | "paused" | "failed" | "cancelled"
  last_chunk_id?: number
  /** 可评价的检索事件关联；仅 assistant 消息会使用此字段。 */
  retrieval_events?: RetrievalReference[]
  /** 助手回合发起或已落库的工具调用，来源于后端 Message.tool_calls。 */
  tool_calls?: ToolCallInvocation[]
  attachments?: AttachmentRef[]
}

export type AttachmentStatus = "uploaded" | "processing" | "ready" | "failed" | "deleted"

export interface AttachmentRef {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
  status: AttachmentStatus
}

export interface Attachment extends AttachmentRef {
  session_id: string | null
  knowledge_base_id?: string | null
  message_id?: string | null
  sha256: string
  adapter_name?: string | null
  adapter_version?: string | null
  capabilities: string[]
  error_message?: string | null
  created_at: string
  updated_at: string
  metadata?: Record<string, unknown>
}

export interface SupportedAttachmentTypes {
  extensions: string[]
}

export interface KnowledgeBase {
  id: string
  name: string
  description: string
  document_count: number
  ready_document_count: number
  total_size_bytes: number
  created_at: string
  updated_at: string
}

export interface ThinkingState {
  active: boolean
  content: string
  messageId: string | null
}

export type ExecutionTimelineStatus = "running" | "completed" | "failed" | "waiting" | "paused" | "cancelled" | "info"

export interface ExecutionTimelineEntry {
  id: string
  session_seq?: number
  run_id?: string
  event_type: string
  label: string
  detail?: string
  status: ExecutionTimelineStatus
  timestamp: string
  metadata?: Record<string, unknown>
}

// ─── 中心编排相关 ────────────────────────────────────────────────

export interface OrchestrationTask {
  plan_id: string
  task_id: string
  title: string
  status: "queued" | "claimed" | "running" | "waiting_approval" | "retry_wait" | "cancel_requested" | "completed" | "failed" | "invalid_output" | "cancelled"
  run_id?: string
  objective?: string
  worker_type?: string
  parent_run_id?: string | null
  execution_generation?: number
  attempt?: number
  worker_run_id?: string
  started_at?: string | null
  completed_at?: string | null
  duration_ms?: number
  result_summary?: string | null
  dependencies?: string[]
  blocked_by?: string[]
  approval_batch_id?: string | null
  error?: string
  error_detail?: ExecutionError | null
  session_seq?: number
  transition_id?: string | null
  metadata?: Record<string, unknown>
}

export type OrchestrationPlanStatus = "created" | "running" | "cancelling" | "completed" | "failed" | "cancelled"

export interface OrchestrationPlan {
  plan_id: string
  run_id?: string
  goal?: string
  status: OrchestrationPlanStatus
  max_parallelism?: number
  task_ids?: string[]
  synthesis_status?: "idle" | "running" | "completed" | "failed"
  session_seq?: number
  transition_id?: string | null
  metadata?: Record<string, unknown>
}

// ─── 会话相关 ───────────────────────────────────────────────────

export interface Session {
  id: string
  title: string
  active_run_id?: string | null
  created_at: string
  updated_at: string
  project_id?: string | null
  last_activity_at?: string | null
  last_message?: string
  message_count?: number
}

export interface SessionMemorySettings {
  session_id: string
  use_memories: boolean
  project_id?: string | null
}

export interface Project {
  id: string
  name: string
  created_at: string
  updated_at: string
}

export interface ChatDraft {
  id: string
  projectId: string
}

export interface SessionRunSummary {
  run_id: string
  session_id: string
  message_id: string
  sequence: number
  status: string
  root_thread_id: string
  error?: unknown
  created_at?: string | null
  updated_at?: string | null
  started_at?: string | null
  title?: string
}

export interface RunListResponse {
  items: SessionRunSummary[]
  total: number
  has_more: boolean
}

export type RunTimelinePayload = Record<string, unknown> & {
  activity_id?: string
  activity_type?: string
  name?: string
  node?: string
  status?: string
  summary?: string
  duration_ms?: number
  parent_activity_id?: string | null
  parent_run_id?: string | null
  error?: { code?: string; message?: string; retryable?: boolean; stage?: string } | null
}

export interface RunTimelineEvent {
  session_seq: number
  event_type: string
  occurred_at: string
  run_id: string | null
  payload: RunTimelinePayload
}

export interface RunTimelineResponse {
  run: SessionRunSummary
  events: RunTimelineEvent[]
  next_after_seq: number
  has_more: boolean
  links: { retrieval_run_ids: string[]; plan_ids: string[]; task_ids: string[] }
}

export interface PlanVersionSummary {
  version: number
  change_reason?: string | null
  based_on_round_id?: string | null
  created_at: string
}

export interface OrchestrationExecutionSnapshot {
  execution_id: string
  attempt: number
  worker_thread_id: string
  status: string
  fencing_token: number | null
  cancellation_epoch: number
  lease_until?: string | null
  result_ref?: string | null
  error?: { code?: string; message?: string; retryable?: boolean; stage?: string } | null
  created_at: string
  updated_at: string
}

export interface OrchestrationTaskSnapshot {
  task_id: string
  title: string
  objective: string
  status: string
  active_execution_id?: string | null
  terminal_at?: string | null
  executions: OrchestrationExecutionSnapshot[]
}

export interface OrchestrationRoundSnapshot {
  round_id: string
  plan_version: number
  round_number: number
  status: string
  expected_task_count: number
  terminal_task_count: number
  created_at: string
  completed_at?: string | null
  tasks: OrchestrationTaskSnapshot[]
}

export interface OrchestrationApprovalSnapshot {
  approval_id: string
  plan_id?: string | null
  task_id?: string | null
  execution_id?: string | null
  interrupt_id?: string | null
  tool_name: string
  risk_level: string
  status: string
  decision?: string | null
  created_at: string
  decided_at?: string | null
}

export interface RunOrchestrationSnapshot {
  run: {
    run_id: string
    session_id: string
    status: string
    root_thread_id: string
    cancellation_epoch: number
    cancellation_external_effect_status: string
    created_at: string
    updated_at: string
  }
  plan?: {
    plan_id: string
    status: string
    user_goal: string
    max_parallelism: number
    current_version: number
    active_round_id?: string | null
    cancellation_epoch: number
    versions: PlanVersionSummary[]
    rounds: OrchestrationRoundSnapshot[]
  } | null
  approvals: OrchestrationApprovalSnapshot[]
}

export interface PlanVersionDetail extends PlanVersionSummary {
  plan_id: string
  dag_json: {
    nodes: Array<Record<string, unknown>>
    edges: Array<Record<string, unknown>>
    [key: string]: unknown
  }
}

// ─── 工具调用相关 ────────────────────────────────────────────────

export type ToolCallStatus = "pending" | "running" | "paused" | "success" | "failed" | "denied" | "timeout"

export interface ToolCall {
  id: string
  tool_name: string
  arguments: Record<string, unknown>
  status: ToolCallStatus
  started_at: string
  completed_at?: string
  duration_ms?: number
  output?: string
  error?: string
  error_stack?: string
  error_detail?: ExecutionError | null
  risk_level: "low" | "medium" | "high"
  /** 实时流由 TOOL_CALL_START 事件补全，用于归组 */
  step_id?: string
  run_id?: string
}

export interface ToolCallCardData {
  id: string
  tool_name: string
  arguments: Record<string, unknown>
  status: ToolCallStatus
  output?: string
  error?: string
  duration_ms?: number
  risk_level: "low" | "medium" | "high"
}

// ─── 审批相关 ────────────────────────────────────────────────────

export type ApprovalAction = "allow" | "deny"

export interface ApprovalRequest {
  approval_id: string
  tool_name: string
  arguments: Record<string, unknown>
  risk_level: "low" | "medium" | "high"
  approval_batch_id?: string | null
  description?: string
  session_id?: string
  run_id?: string
  plan_id?: string
  task_id?: string
  task_title?: string
  worker_run_id?: string
  expires_at?: string
}

export interface ApprovalResult {
  approval_id: string
  decision: "approved" | "denied" | "expired" | "cancelled"
}

// ─── 事件相关 ────────────────────────────────────────────────────

export type { ApplicationEventType }

// ─── 应用状态 ───────────────────────────────────────────────────

export type ConnectionStatus = "connecting" | "connected" | "disconnected" | "error"

export type AgentStatus = "idle" | "queued" | "thinking" | "running" | "cancelling" | "paused" | "waiting_approval" | "completed" | "error"

export interface AppState {
  connectionStatus: ConnectionStatus
  agentStatus: AgentStatus
  activeSessionId: string | null
  pendingApprovals: ApprovalRequest[]
  toolCalls: ToolCall[]
  thinking: ThinkingState | null
  error: string | null
}

// ─── API 响应相关 ────────────────────────────────────────────────

export interface CreateSessionResponse {
  id: string
  title: string
  active_run_id: string | null
  created_at: string
  updated_at?: string
  project_id: string
  last_activity_at?: string | null
}

export interface SubmitRunResponse {
  session_id: string
  run_id: string
  message_id: string
  sequence: number
  status: string
}

export interface ListSessionsResponse {
  sessions: Session[]
}

export interface GetMessagesResponse {
  messages: Message[]
}

export type RetrievalScope = "context" | "memory" | "knowledge" | "file"
export type RetrievalRunStatus =
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "timeout"
  | "skipped"
  | "cancelled"

export interface RetrievalHit {
  channel: string
  rank: number
  score: number | null
}

export interface RetrievalChannelSummary {
  channel: string
  status: string
  hit_count: number
  error_message?: string | null
}

export interface RetrievalProviderSummary {
  provider: string
  query?: string | null
  resource_scope: Record<string, unknown>
  effective_params: Record<string, unknown>
  status: RetrievalRunStatus | string
  duration_ms: number
  error_message?: string | null
  channels: RetrievalChannelSummary[]
  candidate_count: number
  selected_count: number
}

export interface RetrievalCandidate {
  candidate_id: string
  run_id: string
  provider: string
  stage: string
  source_type: string
  source_id: string
  logical_source_id?: string | null
  revision_id?: string | null
  native_rank?: number | null
  native_score?: number | null
  fused_rank?: number | null
  fused_score?: number | null
  rerank_rank?: number | null
  rerank_score?: number | null
  filter_reason?: string | null
  selected_for_result: boolean
  hits: RetrievalHit[]
  context_selected: boolean | null
  selection_stage?: string | null
  content_preview?: string | null
  source_title?: string | null
  locator?: Record<string, unknown>
  metadata: Record<string, unknown>
  created_at: string
}

export interface RetrievalRunSummary {
  run_id: string
  session_id?: string | null
  agent_run_id?: string | null
  message_id?: string | null
  query: string
  scope: RetrievalScope | string
  status: RetrievalRunStatus | string
  trace_status: string
  raw_hit_count: number | null
  candidate_count: number
  selected_count: number
  context_count: number | null
  selection_status: string
  providers: RetrievalProviderSummary[]
  config: Record<string, unknown>
  index_generation?: string | null
  error_message?: string | null
  duration_ms?: number | null
  created_at: string
  completed_at?: string | null
}

export interface RetrievalRunDetail extends RetrievalRunSummary {
  candidates: RetrievalCandidate[]
}

export interface RetrievalRunListResponse {
  items: RetrievalRunSummary[]
  total: number
  limit: number
  offset: number
}

// ─── 应用视图（导航栏）──────────────────────────────────────────

export type AppView =
  | "chat"
  | "knowledge"
  | "memory"
  | "tools"
  | "approvals"
  | "providers"
  | "session-detail"
  | "run-chain"
  | "settings"
  | "mcp"
  | "retrieval"
  | "evaluation"

export interface EvaluationRunSummary {
  run_id: string
  dataset_version: string
  mode: string
  status: string
  name: string
  case_count: number
  completed_count: number
  failed_count: number
  started_at: string | null
  completed_at: string | null
}

export interface EvaluationMetric {
  metric_id: string
  metric_name: string
  case_id: string | null
  value: number | null
  value_json: Record<string, unknown>
  evidence: string[]
}

export interface EvaluationCaseResult {
  case_id: string
  status: string
  attempt: number
  error: string | null
  output: string
  ranking: string[]
  retrieval_run_id: string | null
  agent_run_id: string | null
  duration_ms: number | null
  metrics: EvaluationMetric[]
}

export interface EvaluationRunDetail extends EvaluationRunSummary {
  config: Record<string, unknown>
  metrics: EvaluationMetric[]
  gates: Array<{ metric: string; status: string; actual: number | null; baseline: number | null; op: string; threshold: number; reason: string | null }>
  cases: EvaluationCaseResult[]
}

// ─── 工具管理 ────────────────────────────────────────────────────

export interface ToolInfo {
  name: string
  description: string
  risk_level: "low" | "medium" | "high"
  execution_mode: "native" | "mcp"
  require_approval: boolean
  enabled: boolean
  parameters: Record<string, unknown>
  last_called_at: string | null
}

export interface ToolListResponse {
  items: ToolInfo[]
  total: number
  enabled: number
  high_risk: number
  calls_today: number
}

// ─── 记忆管理 ────────────────────────────────────────────────────

export interface MemoryEntry {
  id: string
  content: string
  metadata: Record<string, unknown>
  expires_at: string | null
  created_at: string
  last_accessed_at: string | null
  last_observed_at: string | null
  valid_until: string | null
  validity_status: string
  access_count: number
}

export interface MemoryRelationEntry {
  id: string
  kind: "supersedes" | "contradicts"
  related_memory_id: string
  related_content: string
  related_status: string
  created_at: string
}

export interface MemoryEpisodeEntry {
  episode_id: string
  turn_id: string
  session_id: string
  project_id: string | null
  content: string
  summary: {
    title: string
    goal: string
    outcome: "completed" | "partial" | "failed" | "cancelled" | "unknown"
    result: string
    attempts: string
    reusable_learnings: string[]
    unresolved: string[]
  }
  source_refs: Array<{
    source_type: string
    source_id: string
    excerpt: string
    observed_at: string
  }>
  source_fingerprint: string
  created_at: string
}

export interface MemoryEpisodeListResponse {
  items: MemoryEpisodeEntry[]
  total: number
}

export interface MemoryJobStatus {
  turn_id: string
  session_id: string
  project_id: string | null
  status: string
  attempt: number
  created_at: string
  updated_at: string
  available_at: string
}

export interface MemoryJobListResponse {
  items: MemoryJobStatus[]
  total: number
  limit: number
  offset: number
}

export interface MemoryListResponse {
  items: MemoryEntry[]
  total: number
  expired: number
  recent_week: number
}

// ─── 审批日志 ────────────────────────────────────────────────────

export interface ApprovalHistoryEntry {
  approval_id: string
  session_id: string
  tool_call_id: string
  tool_name: string
  arguments: Record<string, unknown>
  risk_level: "low" | "medium" | "high"
  created_at: string | null
  expires_at: string | null
  resolved: boolean
  resolution: string
  run_id: string
  plan_id?: string | null
  task_id?: string | null
}

export interface ApprovalStats {
  pending: number
  approved: number
  denied: number
  total: number
}

// ─── LLM 提供商 ──────────────────────────────────────────────────

export interface ProviderInfo {
  name: string
  provider: string
  model: string
  base_url: string
  api_key_configured: boolean
  api_key_masked: string
  temperature: number
  max_tokens: number
}

export interface TestProviderResult {
  ok: boolean
  latency_ms: number | null
  error: string | null
}

// ─── MCP 服务器 ─────────────────────────────────────────────────

export interface McpServerConfig {
  command: string
  args: string[]
  env: Record<string, string>
}

export interface McpRegisterPayload {
  /** 与用户输入的 mcpServers 格式一致 */
  mcpServers: Record<string, McpServerConfig>
}

export interface McpServerInfo {
  name: string
  command: string
  args: string[]
  /** env 值仅返回掩码（如 ••••xxxx），不返回原文 */
  env_masked: Record<string, string>
  status: "connected" | "failed"
  tool_count: number
  error: string | null
  created_at: string
}

export interface McpServerListResponse {
  items: McpServerInfo[]
  total: number
}

export interface McpRegisterResult {
  name: string
  status: "connected" | "failed"
  tool_count: number
  error: string | null
}

export interface McpRegisterResponse {
  total: number
  registered: number
  failed: number
  results: McpRegisterResult[]
}

// ─── 系统设置（只读）────────────────────────────────────────────

export interface SettingsView {
  host: string
  port: number
  debug: boolean
  postgres_user: string
  postgres_db: string
  postgres_host: string
  postgres_port: number
  chromadb_path: string
  max_turns_per_run: number
  retry_budget: number
  tool_timeout: number
  llm_timeout: number
  llm_temperature: number
  llm_max_tokens: number
  memory_ttl_days: number
  memory_min_score: number
  summary_threshold: number
  memory_sync_interval: number
  max_context_tokens: number
  compression_threshold: number
  keep_recent_turns: number
  max_summary_tokens: number
  approval_batch_mode: string
  approval_keyboard_shortcuts: boolean
}
