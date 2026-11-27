import type { ApiSchemas } from "../../shared/api/client";

// 直连 OpenAPI 对话消息、Run 状态机、工具调用与子智能体契约。
export type MessageRole = ApiSchemas["ConversationRole"];
export type MessageStatus = ApiSchemas["MessageStatus"];
export type RunStatus = ApiSchemas["RunStatus"];
export type ToolKind = ApiSchemas["ToolKind"];
export type ToolOutcomeStatus = ApiSchemas["ToolOutcomeStatus"];
export type ToolResultStatus = ApiSchemas["ToolResultStatus"];

export type TextMessagePart = ApiSchemas["TextPartResponse"];
export type FileRefMessagePart = ApiSchemas["FileRefPartResponse"];
export type ToolCallMessagePart = ApiSchemas["ToolCallPartResponse"];
export type ToolResultMessagePart = ApiSchemas["ToolResultPartResponse"];
export type ConversationMessagePart = ApiSchemas["MessagePartResponse"];
export type ConversationMessage = ApiSchemas["ConversationMessageResponse"];
export type ConversationMessagePage =
  ApiSchemas["ConversationMessageListResponse"];

export type RunUsageAttempt = ApiSchemas["RunAttemptUsageResponse"];
export type RunUsage = ApiSchemas["RunUsageResponse"];
export type RunPendingInput = ApiSchemas["RunInputRequestResponse"];
export type RunInputInteraction = ApiSchemas["RunInputInteractionResponse"];
export type FormattedToolArgument = ApiSchemas["FormattedToolArgumentResponse"];
export type RunPendingApproval = ApiSchemas["RunPendingToolApprovalResponse"];
export type SafeSkillInputFile = ApiSchemas["SafeSkillInputFileResponse"];
export type SkillParameterSummaryItem =
  ApiSchemas["SkillParameterSummaryItemResponse"];
export type SafeSkillApprovalProjection =
  ApiSchemas["SafeSkillApprovalProjectionResponse"];
export type RunSkillEntry = ApiSchemas["RunSkillEntryResponse"];
export type RunModelSnapshot = ApiSchemas["RunModelSnapshotResponse"];
export type RunSkillSnapshot = ApiSchemas["RunSkillSnapshotResponse"];
export type SubagentTaskSummary = ApiSchemas["SubagentTaskResponse"];
export type PendingSubagentInput = ApiSchemas["PendingSubagentInputResponse"];
export type PendingSubagentApproval =
  ApiSchemas["PendingSubagentApprovalResponse"];
export type Run = ApiSchemas["RunResponse"];
export type RunListResponse = ApiSchemas["RunListResponse"];
export type RunEvent = ApiSchemas["RunEventResponse"];
export type RunEventListResponse = ApiSchemas["RunEventListResponse"];
export type RunToolOutcome = ApiSchemas["RunToolOutcomeResponse"];
export type RunToolCallSummary = ApiSchemas["RunToolCallSummaryResponse"];
export type RunToolCallDetail = ApiSchemas["RunToolCallDetailResponse"];
export type RunToolCallListResponse = ApiSchemas["RunToolCallListResponse"];

export const BUILTIN_TOOL_DISPLAY_NAMES: Record<string, string> = {
  ask_user_input: "请求补充信息",
  builtin_ask_user_input: "请求补充信息",
  builtin_dispatch_subagents: "协调协作助手",
  builtin_knowledge_search: "查阅企业知识库",
  builtin_request_user_input: "请求补充信息",
  builtin_save_user_memory: "记录个人工作偏好",
  builtin_session_file_list: "查看会话文件",
  builtin_session_file_read: "读取会话文件",
  builtin_skill_execute: "执行技能脚本",
  builtin_skill_load: "加载技能指南",
  builtin_skill_read_resource: "读取技能资源",
  dispatch_subagents: "协调协作助手",
  knowledge_search: "查阅企业知识库",
  request_user_input: "请求补充信息",
  save_user_memory: "记录个人工作偏好",
  session_file_list: "查看会话文件",
  session_file_read: "读取会话文件",
  skill_execute: "执行技能脚本",
  skill_load: "加载技能指南",
  skill_read_resource: "读取技能资源",
};

function pickSafeExplicitDisplayName(
  candidate: string | null | undefined,
  modelName: string,
): string | null {
  const trimmed = candidate?.trim();
  if (!trimmed) {
    return null;
  }
  const normalized = trimmed.toLowerCase();
  // 纵深防御：若服务端/历史载荷中的 display_name 等同底层 model_name 或以 mcp__ 开头，视为无效别名并走安全兜底。
  if (
    normalized === modelName.trim().toLowerCase() ||
    normalized.startsWith("mcp__")
  ) {
    return null;
  }
  return trimmed;
}

/**
 * 解析普通用户可见的工具展示名称：
 * 优先采用管理员/服务端下发的有效业务展示名，其次匹配内置工具中文映射；
 * 若仍无名称且属于 MCP 工具（tool_kind === "mcp" 或 model_name 以 mcp__ 开头），
 * 兜底显示「业务工具操作」，防止向普通用户暴露底层 mcp__xxxx 标识。
 */
export function resolveToolDisplayName(
  tool: {
    display_name?: string | null;
    model_name: string;
    tool_id?: string | null;
    tool_kind?: ToolKind | null;
  },
  fallbackDisplayName?: string | null,
): string {
  const explicit =
    pickSafeExplicitDisplayName(tool.display_name, tool.model_name) ??
    pickSafeExplicitDisplayName(fallbackDisplayName, tool.model_name);
  if (explicit) {
    return explicit;
  }
  if (tool.tool_id && Object.hasOwn(BUILTIN_TOOL_DISPLAY_NAMES, tool.tool_id)) {
    return BUILTIN_TOOL_DISPLAY_NAMES[tool.tool_id];
  }
  const normalized = tool.model_name.toLowerCase();
  if (Object.hasOwn(BUILTIN_TOOL_DISPLAY_NAMES, normalized)) {
    return BUILTIN_TOOL_DISPLAY_NAMES[normalized];
  }
  if (tool.tool_kind === "mcp" || normalized.startsWith("mcp__")) {
    return "业务工具操作";
  }
  return tool.model_name;
}

export type RunContextStopReason =
  | "no_compressible_history"
  | "compression_not_configured"
  | "compression_model_unavailable"
  | "compression_budget_exhausted"
  | "compression_input_too_large"
  | "compression_failed"
  | "compression_insufficient"
  | "hard_window_exceeded";

export interface RunFailedEventPayload {
  type: "failed";
  safe_error_code: string;
  report_message_id?: string | null;
  context_stop_reason?: RunContextStopReason | null;
}

export interface RunStreamMessage {
  id: string;
  session_id: string;
  role: MessageRole;
  status: MessageStatus;
  text: string;
  sequence: number;
  created_at: string;
  parts: ConversationMessagePart[];
  /** 以下可空字段在后端 SSE model_dump_json(exclude_none=True) 时会被省略，消费侧统一映射为 null。 */
  run_id?: string | null;
  reply_to_message_id?: string | null;
  completed_at?: string | null;
}

export interface RunSnapshotPayload {
  status: RunStatus;
  event_sequence: number;
  report_message_id: string | null;
  assistant_message_id: string | null;
  messages: RunStreamMessage[];
  events: RunEvent[];
  messages_complete: boolean;
  messages_before_sequence: number | null;
  events_complete: boolean;
  events_after_sequence: number | null;
  /** 快照内是否存在无法解析的消息（如未知 part 类型或契约缺键）；为 true 时必须触发权威 HTTP 补拉，禁止静默丢消息。 */
  has_unparseable_messages?: boolean;
  subagent_tasks?: SubagentTaskSummary[];
  pending_subagent_inputs?: PendingSubagentInput[];
  pending_subagent_approvals?: PendingSubagentApproval[];
}

export const ACTIVE_RUN_STATUSES: ReadonlySet<RunStatus> = new Set([
  "queued",
  "running",
  "waiting_for_input",
  "waiting_for_approval",
  "waiting_for_subagents",
  "cancelling",
]);

export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_for_input: "等待补充",
  waiting_for_approval: "等待审批",
  waiting_for_subagents: "等待子智能体",
  cancelling: "正在取消",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function isActiveRunStatus(status: RunStatus | null | undefined): boolean {
  return status ? ACTIVE_RUN_STATUSES.has(status) : false;
}

export function isTerminalRunStatus(
  status: RunStatus | null | undefined,
): status is "completed" | "failed" | "cancelled" {
  return status === "completed" || status === "failed" || status === "cancelled";
}

export function isSkillTool(
  modelName: string,
  toolId: string | null = null,
): boolean {
  const normalized = modelName.toLowerCase();
  return (
    toolId === "builtin_skill_execute" ||
    toolId === "builtin_skill_load" ||
    toolId === "builtin_skill_read_resource" ||
    normalized === "skill_execute" ||
    normalized === "skill_load" ||
    normalized === "skill_read_resource" ||
    normalized.endsWith("_skill_execute") ||
    normalized.endsWith("_skill_load") ||
    normalized.endsWith("_skill_read_resource")
  );
}

function isCancellationStatus(status: RunStatus): boolean {
  return status === "cancelling" || status === "cancelled";
}

export function runResultIsStale(candidate: Run, current: Run): boolean {
  if (candidate.id !== current.id) {
    return false;
  }
  // 同一 Run 一旦进入取消态，不能再由迟到的审批/回答投影回退。
  if (
    isCancellationStatus(current.status) &&
    !isCancellationStatus(candidate.status)
  ) {
    return true;
  }
  if (candidate.execution_epoch < current.execution_epoch) {
    return true;
  }
  if (candidate.execution_epoch > current.execution_epoch) {
    return false;
  }
  if (candidate.event_sequence < current.event_sequence) {
    return true;
  }
  if (candidate.event_sequence === current.event_sequence) {
    return (
      Date.parse(candidate.updated_at) < Date.parse(current.updated_at)
    );
  }
  return false;
}

// 校验 SSE 快照是否满足单调推进，拒绝用过期序号、取消前状态或非终态覆盖已有更前进的 Run 缓存。
export function shouldApplyStreamSnapshot(
  existing: Run,
  snapshot: RunSnapshotPayload,
): boolean {
  // 1. 终态不可逆：缓存已到达 completed / failed / cancelled 后禁止任何快照回写。
  if (isTerminalRunStatus(existing.status)) {
    return false;
  }
  // 2. 取消中不可回退：进入 cancelling 后拒绝旧的 running/queued/waiting_* 快照。
  if (
    existing.status === "cancelling" &&
    snapshot.status !== "cancelling" &&
    !isTerminalRunStatus(snapshot.status)
  ) {
    return false;
  }
  // 3. 同代次事件序号单调不减，低于现有缓存序号的旧快照一律丢弃。
  if (snapshot.event_sequence < existing.event_sequence) {
    return false;
  }
  return true;
}

export function selectRunProjection(
  currentRunId: string | null,
  latestRun: Run | null,
  runDetail: Run | null,
): Run | null {
  const candidates = [runDetail, latestRun].filter(
    (candidate): candidate is Run =>
      Boolean(candidate && candidate.id === currentRunId),
  );
  return candidates.reduce<Run | null>((current, candidate) => {
    if (!current) {
      return candidate;
    }
    const currentTerminal = isTerminalRunStatus(current.status);
    const candidateTerminal = isTerminalRunStatus(candidate.status);
    if (candidateTerminal !== currentTerminal) {
      return candidateTerminal ? candidate : current;
    }
    if (candidate.execution_epoch !== current.execution_epoch) {
      return candidate.execution_epoch > current.execution_epoch
        ? candidate
        : current;
    }
    if (candidate.event_sequence !== current.event_sequence) {
      return candidate.event_sequence > current.event_sequence
        ? candidate
        : current;
    }
    // 同版本仍保留第一来源的完整 detail，避免精简 list 投影清空 pending_approvals。
    return Date.parse(candidate.updated_at) > Date.parse(current.updated_at)
      ? candidate
      : current;
  }, null);
}

export function messageText(message: ConversationMessage): string {
  return message.parts
    .filter((part): part is TextMessagePart => part.type === "text")
    .map((part) => part.text)
    .join("");
}

export function mergeConversationMessages(
  pages: ConversationMessagePage[],
  runId: string | null,
  snapshotMessages: RunStreamMessage[],
): ConversationMessage[] {
  const bySequence = new Map<number, ConversationMessage>();
  // 页面按“最新到更早”返回；反转页序后整体才能保持 sequence 正序。
  for (const page of [...pages].reverse()) {
    for (const message of page.items) {
      bySequence.set(message.sequence, message);
    }
  }
  // 后端 SSE 快照消息始终携带完整 session_id 与 parts；
  // 仅对因 model_dump_json(exclude_none=True) 省略的可空字段做 null 归零映射。
  for (const message of snapshotMessages) {
    const previous = bySequence.get(message.sequence) ?? null;
    const existing = previous?.id === message.id ? previous : null;
    bySequence.set(message.sequence, {
      completed_at:
        message.completed_at !== undefined ? message.completed_at : null,
      created_at: message.created_at,
      id: message.id,
      parts: message.parts,
      reply_to_message_id:
        message.reply_to_message_id !== undefined
          ? message.reply_to_message_id
          : null,
      role: message.role,
      run_id: message.run_id ?? existing?.run_id ?? runId ?? "",
      sequence: message.sequence,
      session_id: message.session_id,
      status: message.status,
    });
  }
  return Array.from(bySequence.values()).sort(
    (left, right) => left.sequence - right.sequence,
  );
}

