import type { ArtifactFileRef } from "../files/model";

export type MessageRole = "user" | "assistant";

export type RunStatus =
  | "queued"
  | "running"
  | "waiting_for_input"
  | "waiting_for_approval"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled";

export interface TextMessagePart {
  type: "text";
  text: string;
}

export interface FileRefMessagePart {
  type: "file_ref";
  file_id: string;
  filename: string;
  mime_type: string | null;
  size_bytes: number | null;
}

export interface ToolCallMessagePart {
  type: "tool_call";
  tool_call_id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResultMessagePart {
  type: "tool_result";
  tool_call_id: string;
  status: string;
  output: string | null;
  artifact_id: string | null;
  artifacts: ArtifactFileRef[];
}

export type ConversationMessagePart =
  | TextMessagePart
  | FileRefMessagePart
  | ToolCallMessagePart
  | ToolResultMessagePart;

export interface ConversationMessage {
  id: string;
  session_id: string;
  role: string;
  status: string;
  sequence: number;
  parts: ConversationMessagePart[];
  reply_to_message_id: string | null;
  run_id: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface ConversationMessagePage {
  items: ConversationMessage[];
  next_before_sequence: number | null;
}

export interface RunUsageAttempt {
  execution_epoch: number;
  attempt: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  complete: boolean;
  purpose: string;
  operation_id: string | null;
  batch_index: number;
  model_profile_id: string | null;
  duration_ms: number | null;
}

export interface RunUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  complete: boolean;
  attempts: RunUsageAttempt[];
  answer_tokens: number | null;
  compression_tokens: number | null;
}

export interface RunPendingInput {
  id: string;
  question_message_id: string;
  status: string;
  created_at: string;
}

export interface RunPendingApproval {
  call_id: string;
  batch_message_id: string;
  ordinal: number;
  model_name: string;
  tool_kind: string | null;
  requested_at: string;
  safe_skill_projection: SafeSkillApprovalProjection | null;
}

export interface SafeSkillInputFile {
  file_id: string;
  filename: string;
  mime_type: string | null;
  size_bytes: number | null;
  available: boolean;
}

export interface SkillParameterSummaryItem {
  name: string;
  label: string;
  value: string;
}

export interface SafeSkillApprovalProjection {
  call_id: string;
  skill_id: string;
  skill_name: string;
  skill_version_id: string;
  skill_version_label: string;
  script_label: string;
  purpose: string;
  input_files: SafeSkillInputFile[];
  parameter_summary: SkillParameterSummaryItem[];
  hidden_parameter_count: number;
  can_approve: boolean;
  unavailable_reason: string | null;
}

export interface RunSkillEntry {
  skill_id: string;
  slug: string;
  name: string;
  description: string;
  version_id: string;
  version_label: string;
  requires_approval: boolean;
}

export interface RunSkillSnapshot {
  version: number;
  skills: RunSkillEntry[];
  selected_skill_ids: string[];
}

export interface Run {
  id: string;
  session_id: string;
  status: RunStatus;
  trigger_message_id: string;
  context_cutoff_sequence: number;
  retry_of_run_id: string | null;
  model_config_version_id: string;
  prompt_template_version: string;
  event_sequence: number;
  execution_epoch: number;
  queued_at: string;
  first_started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
  cancel_requested_at: string | null;
  safe_error_code: string | null;
  report_message_id: string | null;
  usage: RunUsage;
  pending_input: RunPendingInput | null;
  pending_approvals: RunPendingApproval[];
  skill_snapshot: RunSkillSnapshot | null;
  input_file_ids: string[];
  replayed: boolean;
  retry_warning: string | null;
}

export interface RunListResponse {
  items: Run[];
  next_cursor: string | null;
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

export interface RunEvent {
  id: string;
  run_id: string;
  sequence: number;
  type: string;
  created_at: string;
  payload: Record<string, unknown>;
}

export interface RunEventListResponse {
  items: RunEvent[];
  next_after_sequence: number | null;
}

export interface RunStreamMessage {
  id: string;
  role: MessageRole;
  status: string;
  text: string;
  sequence: number;
  created_at: string;
}

export interface RunSnapshotPayload {
  status: RunStatus;
  event_sequence: number;
  report_message_id: string | null;
  messages: RunStreamMessage[];
  events: RunEvent[];
  messages_complete: boolean;
  messages_before_sequence: number | null;
  events_complete: boolean;
  events_after_sequence: number | null;
}

export interface RunToolOutcome {
  status: string;
  code: string;
  message: string;
  data: Record<string, unknown>;
  truncated: boolean;
  cleanup_error_code: string | null;
}

export interface RunToolCallSummary {
  id: string;
  tool_call_id: string;
  ordinal: number;
  tool_id: string | null;
  tool_kind: string | null;
  model_name: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  safe_error_code: string | null;
  truncated: boolean;
}

export interface RunToolCallDetail extends RunToolCallSummary {
  arguments: Record<string, unknown> | null;
  outcome: RunToolOutcome | null;
  artifacts: ArtifactFileRef[];
  safe_skill_projection: SafeSkillApprovalProjection | null;
}

export interface RunToolCallListResponse {
  items: RunToolCallSummary[];
  next_after_ordinal: number | null;
}

export const ACTIVE_RUN_STATUSES: ReadonlySet<RunStatus> = new Set([
  "queued",
  "running",
  "waiting_for_input",
  "waiting_for_approval",
  "cancelling",
]);

export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_for_input: "等待补充",
  waiting_for_approval: "等待审批",
  cancelling: "正在取消",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

export function isActiveRunStatus(status: RunStatus | null | undefined): boolean {
  return status ? ACTIVE_RUN_STATUSES.has(status) : false;
}

export function isTerminalRunStatus(status: RunStatus | null | undefined): boolean {
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

function mergeSnapshotTextParts(
  existing: ConversationMessage | null,
  text: string,
): ConversationMessagePart[] {
  if (!existing) {
    return text ? [{ text, type: "text" }] : [];
  }

  const parts: ConversationMessagePart[] = [];
  let textWritten = false;
  for (const part of existing.parts) {
    if (part.type === "text") {
      // 快照只提供文本投影；同一条历史消息仍保留其余类型化内容块。
      if (!textWritten && text) {
        parts.push({ text, type: "text" });
        textWritten = true;
      }
      continue;
    }
    parts.push(part);
  }
  if (text && !textWritten) {
    parts.unshift({ text, type: "text" });
  }
  return parts;
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
  // SSE 快照权威更新文本，但同一消息的文件引用、工具调用和关联字段
  // 来自历史接口，不能在仅有文本投影的快照里被覆盖丢失。
  for (const message of snapshotMessages) {
    const previous = bySequence.get(message.sequence) ?? null;
    const existing = previous?.id === message.id ? previous : null;
    bySequence.set(message.sequence, {
      ...(existing ?? {
        completed_at: null,
        reply_to_message_id: null,
        run_id: runId,
        session_id: "",
      }),
      created_at: message.created_at,
      id: message.id,
      parts: mergeSnapshotTextParts(existing, message.text),
      role: message.role,
      sequence: message.sequence,
      status: message.status,
    });
  }
  return Array.from(bySequence.values()).sort(
    (left, right) => left.sequence - right.sequence,
  );
}

