import type { EventType } from "./events"

// ─── 执行步骤相关 ───────────────────────────────────────────────

export type StepType = "llm_call" | "tool_execution"

export type StepStatus = "pending" | "running" | "completed" | "failed"

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
}

// ─── 消息相关 ───────────────────────────────────────────────────

export type MessageRole = "user" | "assistant" | "system" | "tool"

export interface ToolCallInvocation {
  id: string
  name: string
  args: Record<string, unknown>
}

export interface Message {
  id: string
  role: MessageRole
  content: string
  timestamp: string
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
  /** assistant 回合发起/已落库的工具调用（来自后端 Message.tool_calls） */
  tool_calls?: ToolCallInvocation[]
  attachments?: AttachmentRef[]
}

export type AttachmentStatus = "uploaded" | "queued" | "processing" | "ready" | "failed" | "deleted"

export interface AttachmentRef {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
  status: AttachmentStatus
}

export interface Attachment extends AttachmentRef {
  session_id: string
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

export type FileTaskStatus = "queued" | "running" | "waiting" | "completed" | "failed" | "cancelled"

export interface FileTask {
  id: string
  session_id: string
  attachment_id: string
  task_type: string
  status: FileTaskStatus
  progress: number
  stage: string
  error_message?: string | null
}

export interface AttachmentUploadItem {
  attachment: Attachment
  task: FileTask
}

export interface SupportedAttachmentTypes {
  extensions: string[]
}

export interface ThinkingState {
  active: boolean
  content: string
  messageId: string | null
}

// ─── 会话相关 ───────────────────────────────────────────────────

export interface Session {
  id: string
  title: string
  status: "idle" | "running" | "interrupted" | "completed"
  created_at: string
  updated_at: string
  last_message?: string
  message_count?: number
}

// ─── 工具调用相关 ────────────────────────────────────────────────

export type ToolCallStatus = "pending" | "running" | "success" | "failed" | "denied" | "timeout"

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
  timeout: number
  description?: string
  session_id?: string
}

export interface ApprovalResult {
  approval_id: string
  decision: "approved" | "denied" | "timeout"
}

// ─── 事件相关 ────────────────────────────────────────────────────

export interface WebSocketEvent {
  type: string
  session_id?: string
  run_id?: string
  timestamp: string
  data: Record<string, unknown>
}

export type { EventType }

// ─── 应用状态 ───────────────────────────────────────────────────

export type ConnectionStatus = "connecting" | "connected" | "disconnected" | "error"

export type AgentStatus = "idle" | "thinking" | "running" | "waiting_approval" | "completed" | "error"

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
  status: string
  created_at: string
}

export interface ListSessionsResponse {
  sessions: Session[]
}

export interface GetMessagesResponse {
  messages: Message[]
}

// ─── 应用视图（导航栏）──────────────────────────────────────────

export type AppView =
  | "chat"
  | "memory"
  | "tools"
  | "approvals"
  | "providers"
  | "session-detail"
  | "settings"
  | "mcp"

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
  pinned: boolean
  expires_at: string | null
  created_at: string
  last_accessed: string | null
  access_count: number
}

export interface MemoryListResponse {
  items: MemoryEntry[]
  total: number
  pinned: number
  expired: number
  recent_week: number
}

// ─── 审批日志 ────────────────────────────────────────────────────

export type ApprovalDecision = "approved" | "denied" | "timeout"

export interface ApprovalLog {
  id: string
  session_id: string
  tool_call_id: string
  tool_name: string
  arguments: Record<string, unknown>
  risk_level: "low" | "medium" | "high"
  decision: ApprovalDecision
  decision_time_ms: number
  timestamp: string
}

export interface ApprovalStats {
  today_total: number
  today_approved: number
  today_denied: number
  today_timeout: number
  approval_rate: number
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
  sqlite_db_path: string
  chromadb_path: string
  max_turns_per_run: number
  retry_budget: number
  tool_timeout: number
  llm_stream_timeout: number
  approval_timeout: number
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
  sandbox_enabled: boolean
  sandbox_image: string
  sandbox_network_disabled: boolean
  approval_batch_mode: string
  approval_keyboard_shortcuts: boolean
}
