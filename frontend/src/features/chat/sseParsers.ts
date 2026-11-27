import type { ArtifactFileRef } from "../files/model";
import type {
  ConversationMessagePart,
  FormattedToolArgument,
  MessageRole,
  MessageStatus,
  PendingSubagentApproval,
  PendingSubagentInput,
  RunEvent,
  RunSnapshotPayload,
  RunStatus,
  RunStreamMessage,
  SafeSkillApprovalProjection,
  SafeSkillInputFile,
  SkillParameterSummaryItem,
  SubagentTaskSummary,
  ToolKind,
  ToolResultStatus,
} from "./model";

type JsonRecord = Record<string, unknown>;

// 封闭枚举白名单：对齐后端 OpenAPI 契约，运行时精确校验，拒绝未知值渗透至状态层。
const RUN_STATUSES: readonly RunStatus[] = [
  "queued",
  "running",
  "waiting_for_input",
  "waiting_for_approval",
  "waiting_for_subagents",
  "cancelling",
  "completed",
  "failed",
  "cancelled",
];
const MESSAGE_ROLES: readonly MessageRole[] = ["user", "assistant"];
const MESSAGE_STATUSES: readonly MessageStatus[] = [
  "pending",
  "streaming",
  "completed",
  "failed",
  "cancelled",
];
const TOOL_RESULT_STATUSES: readonly ToolResultStatus[] = ["success", "error"];
const TOOL_KINDS: readonly ToolKind[] = ["builtin", "mcp"];

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** 解析单帧 SSE JSON 负载；语法损坏或非对象结构时返回 null 交由上层触发断流恢复。 */
export function parseEventData(raw: string): JsonRecord | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

const str = (r: JsonRecord, k: string): string | null =>
  typeof r[k] === "string" ? r[k] : null;
const nonEmptyStr = (r: JsonRecord, k: string): string | null =>
  typeof r[k] === "string" && r[k] ? r[k] : null;
const strOr = (r: JsonRecord, k: string, fallback: string): string =>
  typeof r[k] === "string" ? r[k] : fallback;
const num = (r: JsonRecord, k: string): number | null =>
  typeof r[k] === "number" && Number.isFinite(r[k]) ? r[k] : null;
const numOr = (r: JsonRecord, k: string, fallback: number): number =>
  num(r, k) ?? fallback;
const boolOr = (r: JsonRecord, k: string, fallback: boolean): boolean =>
  typeof r[k] === "boolean" ? r[k] : fallback;
const readEnum = <T extends string>(v: unknown, allowed: readonly T[]): T | null =>
  typeof v === "string" ? (allowed.find((item) => item === v) ?? null) : null;

/** 校验目标为对象并批量提取必填字符串字段，任一缺失或非字符串即返回 null。 */
function reqStrings<const K extends readonly string[]>(
  value: unknown,
  keys: K,
): Record<K[number], string> | null {
  if (!isRecord(value)) return null;
  const extracted: Record<string, string> = {};
  for (const key of keys) {
    if (typeof value[key] !== "string") return null;
    extracted[key] = value[key];
  }
  return extracted as Record<K[number], string>;
}

/** 严格数组解析（fail-closed）：非数组或任一元素失败时整体返回 null。 */
function parseStrictArray<T>(
  value: unknown,
  parser: (item: unknown) => T | null,
): T[] | null {
  if (!Array.isArray(value)) return null;
  const items: T[] = [];
  for (const item of value) {
    const parsed = parser(item);
    if (parsed === null) return null;
    items.push(parsed);
  }
  return items;
}

/** 分拣数组解析：保留合法项并标记是否存在损坏项；单条损坏不阻断其余合法条目。 */
function parsePartitionedArray<T>(
  value: unknown,
  parser: (item: unknown) => T | null,
): { hasInvalid: boolean; items: T[] } {
  if (!Array.isArray(value)) return { hasInvalid: false, items: [] };
  const items: T[] = [];
  let hasInvalid = false;
  for (const item of value) {
    const parsed = parser(item);
    if (parsed !== null) items.push(parsed);
    else hasInvalid = true;
  }
  return { hasInvalid, items };
}

const parseFilteredArray = <T>(
  value: unknown,
  parser: (item: unknown) => T | null,
): T[] => parsePartitionedArray(value, parser).items;

function parseArtifactFileRef(value: unknown): ArtifactFileRef | null {
  const fields = reqStrings(value, ["file_id", "filename", "mime_type"]);
  const sizeBytes = fields && isRecord(value) ? num(value, "size_bytes") : null;
  if (!fields || sizeBytes === null || !isRecord(value)) return null;
  return { ...fields, label: str(value, "label"), size_bytes: sizeBytes };
}

function parseMessagePart(value: unknown): ConversationMessagePart | null {
  if (!isRecord(value)) return null;
  switch (value.type) {
    case "text": {
      const text = str(value, "text");
      return text !== null ? { text, type: "text" } : null;
    }
    case "file_ref": {
      const fields = reqStrings(value, ["file_id", "filename"]);
      return fields
        ? { ...fields, mime_type: str(value, "mime_type"), size_bytes: num(value, "size_bytes"), type: "file_ref" }
        : null;
    }
    case "tool_call": {
      const fields = reqStrings(value, ["name", "tool_call_id"]);
      return fields
        ? { ...fields, arguments: isRecord(value.arguments) ? value.arguments : {}, type: "tool_call" }
        : null;
    }
    case "tool_result": {
      const toolCallId = str(value, "tool_call_id");
      const status = readEnum(value.status, TOOL_RESULT_STATUSES);
      const artifacts = Array.isArray(value.artifacts)
        ? parseStrictArray(value.artifacts, parseArtifactFileRef)
        : [];
      if (toolCallId === null || !status || !artifacts) return null;
      return { artifacts, output: str(value, "output"), status, tool_call_id: toolCallId, type: "tool_result" };
    }
    default:
      return null;
  }
}

function parseRunStreamMessage(value: unknown): RunStreamMessage | null {
  // 对齐后端 RunStreamMessage 强契约：session_id 与 parts 必填非空；
  // 缺少或包含非法 part 时丢弃该条消息，由快照置位 has_unparseable_messages 触发权威补拉。
  const fields = reqStrings(value, ["created_at", "id", "session_id", "text"]);
  if (!fields || !isRecord(value)) return null;
  const role = readEnum(value.role, MESSAGE_ROLES);
  const status = readEnum(value.status, MESSAGE_STATUSES);
  const sequence = num(value, "sequence");
  const parts = parseStrictArray(value.parts, parseMessagePart);
  if (!role || !status || sequence === null || !parts) return null;
  // 还原后端 SSE model_dump_json(exclude_none=True) 省略的可空字段为 null。
  return {
    ...fields,
    completed_at: str(value, "completed_at"),
    parts,
    reply_to_message_id: str(value, "reply_to_message_id"),
    role,
    run_id: str(value, "run_id"),
    sequence,
    status,
  };
}

const parseFormattedToolArgument = (v: unknown): FormattedToolArgument | null =>
  reqStrings(v, ["key", "label", "value"]);

const parseSkillParameterSummaryItem = (v: unknown): SkillParameterSummaryItem | null =>
  reqStrings(v, ["label", "name", "value"]);

function parseSafeSkillInputFile(value: unknown): SafeSkillInputFile | null {
  const fields = reqStrings(value, ["file_id", "filename"]);
  if (!fields || !isRecord(value)) return null;
  // 显式还原被 exclude_none=True 省略的 mime_type / size_bytes，避免下游按 null 判空时漏判 undefined。
  return {
    ...fields,
    available: boolOr(value, "available", true),
    mime_type: str(value, "mime_type"),
    size_bytes: num(value, "size_bytes"),
  };
}

function parseSafeSkillApprovalProjection(value: unknown): SafeSkillApprovalProjection | null {
  const fields = reqStrings(value, [
    "call_id",
    "purpose",
    "script_label",
    "skill_id",
    "skill_name",
    "skill_version_id",
    "skill_version_label",
  ]);
  if (!fields || !isRecord(value)) return null;
  const inputFiles = value.input_files === undefined
    ? []
    : parseStrictArray(value.input_files, parseSafeSkillInputFile);
  const parameterSummary = value.parameter_summary === undefined
    ? []
    : parseStrictArray(value.parameter_summary, parseSkillParameterSummaryItem);
  if (!inputFiles || !parameterSummary) return null;
  return {
    ...fields,
    // 安全底线（fail-closed）：仅显式布尔值透传，缺失默认禁止批准。
    can_approve: boolOr(value, "can_approve", false),
    hidden_parameter_count: numOr(value, "hidden_parameter_count", 0),
    input_files: inputFiles,
    parameter_summary: parameterSummary,
    unavailable_reason: str(value, "unavailable_reason"),
  };
}

/** 复用提取子智能体任务/待输入/待审批共同的基础定位字段。 */
function parseSubagentBase(record: JsonRecord) {
  const ids = reqStrings(record, ["sub_run_id", "subagent_name", "subagent_slug"]);
  if (!ids) return null;
  return {
    ...ids,
    task_index: numOr(record, "task_index", 1),
    task_summary: strOr(record, "task_summary", ""),
  };
}

function parseSubagentTaskSummary(value: unknown): SubagentTaskSummary | null {
  if (!isRecord(value)) return null;
  const base = parseSubagentBase(value);
  const status = readEnum(value.status, RUN_STATUSES);
  const fields = reqStrings(value, ["created_at", "parent_tool_call_id"]);
  if (!base || !status || !fields) return null;
  return {
    ...base,
    ...fields,
    finished_at: str(value, "finished_at"),
    first_started_at: str(value, "first_started_at"),
    safe_error_code: str(value, "safe_error_code"),
    status,
    subagent_profile_id: str(value, "subagent_profile_id"),
  };
}

function parsePendingSubagentInput(value: unknown): PendingSubagentInput | null {
  if (!isRecord(value)) return null;
  const base = parseSubagentBase(value);
  const fields = reqStrings(value, ["created_at", "input_request_id", "question_message_id", "question_text"]);
  return base && fields ? { ...base, ...fields } : null;
}

function parsePendingSubagentApproval(value: unknown): PendingSubagentApproval | null {
  if (!isRecord(value)) return null;
  const base = parseSubagentBase(value);
  const fields = reqStrings(value, ["batch_message_id", "call_id", "model_name", "requested_at"]);
  const ordinal = num(value, "ordinal");
  if (!base || !fields || ordinal === null) return null;
  // 还原后端 SSE exclude_none=True 递归省略的可空字段与默认空数组，保证审批卡片强契约不被击穿。
  return {
    ...base,
    ...fields,
    approval_summary: str(value, "approval_summary"),
    display_name: str(value, "display_name"),
    formatted_arguments: parseFilteredArray(value.formatted_arguments, parseFormattedToolArgument),
    hidden_argument_count: numOr(value, "hidden_argument_count", 0),
    ordinal,
    safe_skill_projection: value.safe_skill_projection != null
      ? parseSafeSkillApprovalProjection(value.safe_skill_projection)
      : null,
    tool_kind: readEnum(value.tool_kind, TOOL_KINDS),
  };
}

/** 解析单条持久 RunEvent 负载；缺失关键标识或非对象 payload 时丢弃。 */
export function parseRunEvent(value: unknown): RunEvent | null {
  const fields = reqStrings(value, ["created_at", "id", "run_id", "type"]);
  if (!fields || !isRecord(value)) return null;
  const sequence = num(value, "sequence");
  if (sequence === null || !isRecord(value.payload)) return null;
  return { ...fields, payload: value.payload, sequence };
}

/**
 * 解析 SSE 权威 snapshot 帧：
 * - 任一消息解析失败时置位 has_unparseable_messages 且强制 messages_complete=false，通知控制器走权威 HTTP 补拉；
 * - 统一还原后端 model_dump_json(exclude_none=True) 省略的可空字段，防止下游读到 undefined。
 */
export function parseSnapshotPayload(value: unknown): RunSnapshotPayload | null {
  if (!isRecord(value)) return null;
  const status = readEnum(value.status, RUN_STATUSES);
  if (!status) return null;
  const { hasInvalid: hasUnparseableMessages, items: messages } =
    parsePartitionedArray(value.messages, parseRunStreamMessage);
  return {
    assistant_message_id: nonEmptyStr(value, "assistant_message_id"),
    events: parseFilteredArray(value.events, parseRunEvent),
    events_after_sequence: num(value, "events_after_sequence"),
    events_complete: Boolean(value.events_complete),
    event_sequence: numOr(value, "event_sequence", 0),
    has_unparseable_messages: hasUnparseableMessages,
    messages,
    messages_before_sequence: num(value, "messages_before_sequence"),
    messages_complete: hasUnparseableMessages ? false : Boolean(value.messages_complete),
    pending_subagent_approvals: parseFilteredArray(value.pending_subagent_approvals, parsePendingSubagentApproval),
    pending_subagent_inputs: parseFilteredArray(value.pending_subagent_inputs, parsePendingSubagentInput),
    report_message_id: nonEmptyStr(value, "report_message_id"),
    status,
    subagent_tasks: parseFilteredArray(value.subagent_tasks, parseSubagentTaskSummary),
  };
}
