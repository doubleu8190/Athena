import { listMessages } from "./api";
import {
  isTerminalRunStatus,
  type ConversationMessage,
  type ConversationMessagePage,
  type PendingSubagentApproval,
  type PendingSubagentInput,
  type RunEvent,
  type RunSnapshotPayload,
  type RunStatus,
  type RunStreamMessage,
  type SubagentTaskSummary,
} from "./model";

export type RunStreamConnection =
  | "idle"
  | "connecting"
  | "open"
  | "interrupted"
  | "error"
  | "closed";

export interface RunStreamState {
  assistantMessageId: string | null;
  connection: RunStreamConnection;
  events: RunEvent[];
  hasSnapshot: boolean;
  lastHeartbeatAt: string | null;
  messages: RunStreamMessage[];
  messagesComplete: boolean;
  notice: string | null;
  pendingSubagentApprovals: PendingSubagentApproval[];
  pendingSubagentInputs: PendingSubagentInput[];
  reportMessageId: string | null;
  runId: string | null;
  status: RunStatus | null;
  subagentTasks: SubagentTaskSummary[];
  /** 是否已收到终态权威 snapshot；仅收到终态 run_event 预告时保持 false，避免提前切断 SSE 丢弃紧随其后的自包含快照。 */
  terminalSnapshotReceived: boolean;
  transientText: string;
}

export type RunStreamAction =
  | { type: "start"; runId: string; status: RunStatus | null }
  | { type: "sync_status"; runId: string; status: RunStatus }
  | { type: "query_terminal"; runId: string; status: RunStatus }
  | { type: "settle_terminal"; runId: string; status: RunStatus }
  | { type: "open" }
  | { type: "error" }
  | { type: "closed" }
  | { type: "heartbeat"; at: string }
  | { type: "stream_interrupted"; reason: string }
  | { type: "text_delta"; text: string }
  | { type: "run_event"; event: RunEvent }
  | { type: "snapshot"; payload: RunSnapshotPayload };

export const INITIAL_RUN_STREAM_STATE: RunStreamState = {
  assistantMessageId: null,
  connection: "idle",
  events: [],
  hasSnapshot: false,
  lastHeartbeatAt: null,
  messages: [],
  messagesComplete: false,
  notice: null,
  pendingSubagentApprovals: [],
  pendingSubagentInputs: [],
  reportMessageId: null,
  runId: null,
  status: null,
  subagentTasks: [],
  terminalSnapshotReceived: false,
  transientText: "",
};

function eventStatus(type: string): RunStatus | null {
  switch (type) {
    case "queued":
    case "approval_resumed":
    case "resumed":
      return "queued";
    case "started":
      return "running";
    case "waiting_for_input":
      return "waiting_for_input";
    case "waiting_for_approval":
      return "waiting_for_approval";
    case "waiting_for_subagents":
      return "waiting_for_subagents";
    case "cancel_requested":
      return "cancelling";
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    default:
      return null;
  }
}

export function completedEventAssistantMessageId(
  events: readonly RunEvent[],
  runId: string,
): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (
      event.run_id === runId &&
      event.type === "completed" &&
      typeof event.payload.assistant_message_id === "string" &&
      event.payload.assistant_message_id
    ) {
      return event.payload.assistant_message_id;
    }
  }
  return null;
}

export function failedEventReportMessageId(
  events: readonly RunEvent[],
  runId: string,
): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (
      event.run_id === runId &&
      event.type === "failed" &&
      typeof event.payload.report_message_id === "string" &&
      event.payload.report_message_id
    ) {
      return event.payload.report_message_id;
    }
  }
  return null;
}

function interruptedNotice(reason: string): string {
  if (reason === "redis_unavailable") {
    return "实时连接暂时不可用，已保留当前文本，正在等待权威快照重新同步。";
  }
  if (reason === "stream_gap") {
    return "实时事件存在缺口，已保留当前文本，正在重新同步。";
  }
  return "实时事件流已中断，已保留当前文本，正在等待权威快照重新同步。";
}

export function runStreamReducer(
  state: RunStreamState,
  action: RunStreamAction,
): RunStreamState {
  switch (action.type) {
    case "start":
      if (state.runId !== action.runId) {
        return {
          ...INITIAL_RUN_STREAM_STATE,
          connection: "connecting",
          runId: action.runId,
          status: action.status,
        };
      }
      return {
        ...state,
        connection: "connecting",
        status: action.status ?? state.status,
      };
    case "sync_status":
      if (state.runId !== action.runId) {
        return {
          ...INITIAL_RUN_STREAM_STATE,
          runId: action.runId,
          status: action.status,
        };
      }
      return { ...state, status: action.status };
    case "query_terminal":
      if (state.runId !== action.runId) {
        return state;
      }
      return { ...state, connection: "closed", status: action.status };
    case "settle_terminal":
      if (state.runId !== action.runId) {
        return state;
      }
      return {
        ...state,
        connection: "closed",
        notice: null,
        status: action.status,
        transientText: "",
      };
    case "open":
      return {
        ...state,
        connection: "open",
      };
    case "error":
      return {
        ...state,
        connection: "error",
        notice: "实时连接中断，已保留当前文本，正在尝试重新同步。",
      };
    case "closed":
      return { ...state, connection: "closed" };
    case "heartbeat":
      return { ...state, lastHeartbeatAt: action.at };
    case "stream_interrupted":
      return {
        ...state,
        connection: "interrupted",
        notice: interruptedNotice(action.reason),
      };
    case "text_delta":
      if (!action.text) {
        return state;
      }
      return {
        ...state,
        connection: state.connection === "closed" ? state.connection : "open",
        transientText: state.transientText + action.text,
      };
    case "run_event": {
      if (
        state.events.some(
          (event) =>
            event.id === action.event.id ||
            event.sequence === action.event.sequence,
        )
      ) {
        return state;
      }
      const nextEvents = [...state.events, action.event];
      return {
        ...state,
        assistantMessageId:
          action.event.run_id === state.runId &&
          action.event.type === "completed"
            ? completedEventAssistantMessageId(nextEvents, action.event.run_id) ??
              state.assistantMessageId
            : state.assistantMessageId,
        events: nextEvents,
        reportMessageId:
          action.event.run_id === state.runId &&
          action.event.type === "failed"
            ? failedEventReportMessageId(nextEvents, action.event.run_id) ??
              state.reportMessageId
            : state.reportMessageId,
        status: eventStatus(action.event.type) ?? state.status,
      };
    }
    case "snapshot": {
      const authoritativeStatus = action.payload.status;
      const terminalSnapshot = isTerminalRunStatus(authoritativeStatus);
      const snapshotAssistantMessageId =
        action.payload.assistant_message_id ??
        (state.runId
          ? completedEventAssistantMessageId(action.payload.events, state.runId)
          : null);
      const snapshotReportMessageId =
        action.payload.report_message_id ??
        (state.runId
          ? failedEventReportMessageId(action.payload.events, state.runId)
          : null);
      return {
        ...state,
        assistantMessageId: snapshotAssistantMessageId,
        connection:
          authoritativeStatus === "waiting_for_input" ||
          authoritativeStatus === "waiting_for_approval" ||
          terminalSnapshot
            ? "closed"
            : "open",
        events: action.payload.events,
        hasSnapshot: true,
        messages: action.payload.messages,
        messagesComplete: action.payload.messages_complete,
        // 非终态快照代表可继续生成的权威进度；终态快照由控制器确认自包含落盘或单次兜底后再显式 settle 清理临时文本。
        transientText: terminalSnapshot ? state.transientText : "",
        notice: null,
        pendingSubagentApprovals:
          action.payload.pending_subagent_approvals ?? [],
        pendingSubagentInputs: action.payload.pending_subagent_inputs ?? [],
        reportMessageId: snapshotReportMessageId,
        status: authoritativeStatus,
        subagentTasks: action.payload.subagent_tasks ?? [],
        terminalSnapshotReceived: terminalSnapshot,
      };
    }
    default:
      return state;
  }
}

export type RunStreamMode = "live" | "waiting" | null;

export function runStreamMode(
  status: RunStatus | null | undefined,
): RunStreamMode {
  if (
    status === "queued" ||
    status === "running" ||
    status === "waiting_for_subagents" ||
    status === "cancelling"
  ) {
    return "live";
  }
  if (
    status === "waiting_for_input" ||
    status === "waiting_for_approval"
  ) {
    return "waiting";
  }
  return null;
}

interface ProjectRunStatusInput {
  queryStatus: RunStatus | null;
  runId: string | null;
  streamRunId: string | null;
  streamStatus: RunStatus | null;
}

export function projectRunStatus({
  queryStatus,
  runId,
  streamRunId,
  streamStatus,
}: ProjectRunStatusInput): RunStatus | null {
  // 终态不可逆，查询已确认后必须覆盖可能漏掉完成事件的 SSE 残留状态。
  if (queryStatus && isTerminalRunStatus(queryStatus)) {
    return queryStatus;
  }
  // 非终态仍优先采用 SSE，让 started、取消请求等状态先于查询显示。
  if (runId && streamRunId === runId && streamStatus) {
    return streamStatus;
  }
  return queryStatus;
}

/**
 * 将后端自包含 SSE 快照消息转换为与 /messages 接口同构的 ConversationMessage。
 * 后端 SSE 使用 model_dump_json(exclude_none=True)，值为 null 的可空字段会在 JSON 中省略，此处统一映射为 null。
 */
export function toConversationMessageFromSnapshot(
  message: RunStreamMessage,
  fallbackRunId: string,
): ConversationMessage {
  return {
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
    run_id: message.run_id ?? fallbackRunId,
    sequence: message.sequence,
    session_id: message.session_id,
    status: message.status,
  };
}

export interface TerminalSnapshotResolution {
  /** 快照中可直接并入消息缓存的全部消息。 */
  messages: ConversationMessage[];
  /** 命中的权威终态消息（completed 最终回答或 failed 安全停止报告）。 */
  targetMessage: ConversationMessage;
}

/**
 * 校验终态 SSE 快照是否自包含完整目标消息，命中后可直接落入 TanStack Query
 * 缓存并清理 transientText，实现零额外 /events 与 /messages HTTP 往返；
 * 若因 >512KB 快照裁剪导致目标消息未包含，返回 null 交由 fetchTerminalMessagesFallback 单次兜底。
 */
export function resolveSelfContainedTerminalSnapshot(
  snapshot: RunSnapshotPayload,
  runId: string,
  triggerMessageId: string | null = null,
): TerminalSnapshotResolution | null {
  if (
    (snapshot.status !== "completed" && snapshot.status !== "failed") ||
    snapshot.has_unparseable_messages
  ) {
    // 终态快照内若存在无法解析的消息（如未知 part 类型），禁止内联收敛，
    // 交由单次有界 fetchTerminalMessagesFallback 拉取完整权威消息，避免漏消息或误匹配早先澄清助手消息。
    return null;
  }

  const convertedMessages: ConversationMessage[] = [];
  const convertedById = new Map<string, ConversationMessage>();
  for (const raw of snapshot.messages) {
    const converted = toConversationMessageFromSnapshot(raw, runId);
    convertedMessages.push(converted);
    convertedById.set(converted.id, converted);
  }
  if (convertedMessages.length === 0) {
    return null;
  }

  if (snapshot.status === "completed") {
    const expectedId =
      snapshot.assistant_message_id ??
      completedEventAssistantMessageId(snapshot.events, runId);
    if (expectedId) {
      const matched = convertedById.get(expectedId);
      if (
        matched &&
        matched.role === "assistant" &&
        matched.run_id === runId &&
        matched.status === "completed"
      ) {
        return { messages: convertedMessages, targetMessage: matched };
      }
      // 已明确给出目标 ID 但快照未包含（如超 512KB 被裁剪），不得误用旧交付消息。
      return null;
    }

    // 未显式携带 assistant_message_id 时，仅接受显式归属本 Run 的最终已完成助手消息。
    for (let index = snapshot.messages.length - 1; index >= 0; index -= 1) {
      const raw = snapshot.messages[index];
      if (
        raw.role === "assistant" &&
        raw.run_id === runId &&
        raw.status === "completed"
      ) {
        const matched = convertedById.get(raw.id);
        if (matched) {
          return { messages: convertedMessages, targetMessage: matched };
        }
      }
    }
    return null;
  }

  // failed 终态仅当显式携带安全停止报告 ID 且快照内含完整报告消息时才直接收敛。
  const reportMessageId =
    snapshot.report_message_id ??
    failedEventReportMessageId(snapshot.events, runId);
  if (!reportMessageId) {
    return null;
  }
  const reportMessage = convertedById.get(reportMessageId);
  if (
    !reportMessage ||
    reportMessage.role !== "assistant" ||
    reportMessage.run_id !== runId ||
    reportMessage.status !== "completed"
  ) {
    return null;
  }
  if (
    triggerMessageId &&
    reportMessage.reply_to_message_id !== null &&
    reportMessage.reply_to_message_id !== triggerMessageId
  ) {
    return null;
  }
  return { messages: convertedMessages, targetMessage: reportMessage };
}

export interface TerminalTargetMatcher {
  assistantMessageId?: string | null;
  reportMessageId?: string | null;
  runId: string;
  status: "completed" | "failed";
  triggerMessageId?: string | null;
}

export interface BoundedTerminalFallbackResult {
  page: ConversationMessagePage;
  targetFound: boolean;
}

/** 校验消息集合中是否已存在当前终态 Run 的权威目标消息。 */
export function hasTerminalTargetMessage(
  messages: readonly ConversationMessage[],
  matcher: TerminalTargetMatcher,
): boolean {
  if (matcher.status === "completed") {
    if (matcher.assistantMessageId) {
      return messages.some(
        (message) =>
          message.id === matcher.assistantMessageId &&
          message.role === "assistant" &&
          message.run_id === matcher.runId &&
          message.status === "completed",
      );
    }
    return messages.some(
      (message) =>
        message.role === "assistant" &&
        message.run_id === matcher.runId &&
        message.status === "completed",
    );
  }

  if (!matcher.reportMessageId) {
    return false;
  }
  const triggerMessage = matcher.triggerMessageId
    ? messages.find((message) => message.id === matcher.triggerMessageId)
    : undefined;
  return messages.some(
    (message) =>
      message.id === matcher.reportMessageId &&
      message.role === "assistant" &&
      message.run_id === matcher.runId &&
      message.status === "completed" &&
      (!matcher.triggerMessageId ||
        message.reply_to_message_id === matcher.triggerMessageId) &&
      (!triggerMessage || message.sequence > triggerMessage.sequence),
  );
}

/**
 * 终态单次有界兜底刷新：
 * 仅在极端 >512KB 快照裁剪、无终态 snapshot 断流或页面刷新无 SSE 场景下调用，
 * 固定发起 1 次 GET /sessions/{id}/messages?limit=20，绝不执行多页 while(true) 翻页或 /events 扫描。
 */
export async function fetchTerminalMessagesFallback(
  sessionId: string,
  matcher: TerminalTargetMatcher,
): Promise<BoundedTerminalFallbackResult> {
  const page = await listMessages(sessionId, undefined, 20);
  return {
    page,
    targetFound: hasTerminalTargetMessage(page.items, matcher),
  };
}
