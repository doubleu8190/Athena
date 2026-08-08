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
  status: StepStatus
  started_at: string
  completed_at?: string | null
  duration_ms: number
  llm_input_tokens: number
  llm_output_tokens: number
  error_message?: string | null
  metadata?: Record<string, unknown>
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
  tool_name?: string
  tool_call_id?: string
  /** assistant 回合发起/已落库的工具调用（来自后端 Message.tool_calls） */
  tool_calls?: ToolCallInvocation[]
  metadata?: Record<string, unknown>
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
  risk_level: "low" | "medium" | "high"
  step_id?: string
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
