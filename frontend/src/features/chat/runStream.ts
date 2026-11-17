import {
  isTerminalRunStatus,
  type ConversationMessagePage,
  type PendingSubagentApproval,
  type PendingSubagentInput,
  type RunEvent,
  type RunSnapshotPayload,
  type RunStatus,
  type RunStreamMessage,
  type SubagentTaskSummary,
} from "./model";
import { listMessages, listRunEvents } from "./api";

export type RunStreamConnection =
  | "idle"
  | "connecting"
  | "open"
  | "interrupted"
  | "error"
  | "closed";

export interface RunStreamState {
  connection: RunStreamConnection;
  events: RunEvent[];
  hasSnapshot: boolean;
  lastHeartbeatAt: string | null;
  messages: RunStreamMessage[];
  notice: string | null;
  pendingSubagentApprovals: PendingSubagentApproval[];
  pendingSubagentInputs: PendingSubagentInput[];
  reportMessageId: string | null;
  runId: string | null;
  status: RunStatus | null;
  subagentTasks: SubagentTaskSummary[];
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
  connection: "idle",
  events: [],
  hasSnapshot: false,
  lastHeartbeatAt: null,
  messages: [],
  notice: null,
  pendingSubagentApprovals: [],
  pendingSubagentInputs: [],
  reportMessageId: null,
  runId: null,
  status: null,
  subagentTasks: [],
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

function failedEventReportMessageId(
  events: RunEvent[],
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
      return {
        ...state,
        events: [...state.events, action.event],
        reportMessageId:
          action.event.run_id === state.runId &&
          action.event.type === "failed"
            ? failedEventReportMessageId(
                [...state.events, action.event],
                action.event.run_id,
              ) ?? state.reportMessageId
            : state.reportMessageId,
        status: eventStatus(action.event.type) ?? state.status,
      };
    }
    case "snapshot": {
      const authoritativeStatus = action.payload.status;
      const terminalSnapshot = isTerminalRunStatus(authoritativeStatus);
      return {
        ...state,
        connection:
          authoritativeStatus === "waiting_for_input" ||
          authoritativeStatus === "waiting_for_approval" ||
          isTerminalRunStatus(authoritativeStatus)
            ? "closed"
            : "open",
        events: action.payload.events,
        hasSnapshot: true,
        messages: action.payload.messages,
        pendingSubagentApprovals:
          action.payload.pending_subagent_approvals ?? [],
        pendingSubagentInputs: action.payload.pending_subagent_inputs ?? [],
        // 非终态快照代表可继续生成的权威进度；终态 snapshot 不含未完成文本，
        // 必须等 completed 或带报告的 failed 精确对账后才能收敛临时文本。
        transientText: terminalSnapshot ? state.transientText : "",
        notice: null,
        reportMessageId: action.payload.report_message_id,
        status: authoritativeStatus,
        subagentTasks: action.payload.subagent_tasks ?? [],
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

export type CompletedRunReconciliation =
  | {
      kind: "settled";
      messages: {
        page: ConversationMessagePage;
        beforeSequence: number | undefined;
      }[];
    }
  | {
      kind: "not_found";
      reason: "completed_event" | "final_message";
    };

export type FailedRunReconciliation =
  | {
      kind: "settled";
      messages: {
        page: ConversationMessagePage;
        beforeSequence: number | undefined;
      }[];
    }
  | {
      kind: "not_found";
      reason: "failed_event" | "report_message";
    };

function completedAssistantMessageId(
  events: RunEvent[],
  runId: string,
): string | null {
  for (const event of events) {
    if (
      event.run_id !== runId ||
      event.type !== "completed" ||
      typeof event.payload.assistant_message_id !== "string" ||
      !event.payload.assistant_message_id
    ) {
      continue;
    }
    return event.payload.assistant_message_id;
  }
  return null;
}

async function findCompletedAssistantMessageId(
  sessionId: string,
  runId: string,
): Promise<string | null> {
  let afterSequence = 0;
  while (true) {
    const page = await listRunEvents(sessionId, runId, afterSequence, 100);
    const assistantMessageId = completedAssistantMessageId(
      page.items,
      runId,
    );
    if (assistantMessageId) {
      return assistantMessageId;
    }
    const next = page.next_after_sequence;
    // 游标不前进说明接口已耗尽或返回异常，不能猜测 latest assistant 回退。
    if (next === null || next <= afterSequence) {
      return null;
    }
    afterSequence = next;
  }
}

async function finalAssistantMessageExists(
  sessionId: string,
  runId: string,
  assistantMessageId: string,
  triggerMessageId: string,
): Promise<{
  found: boolean;
  pages: {
    page: ConversationMessagePage;
    beforeSequence: number | undefined;
  }[];
}> {
  let beforeSequence: number | undefined;
  const pages: {
    page: ConversationMessagePage;
    beforeSequence: number | undefined;
  }[] = [];
  while (true) {
    const page = await listMessages(sessionId, beforeSequence, 20);
    pages.push({ page, beforeSequence });
    if (
      page.items.some(
        (message) =>
          message.id === assistantMessageId &&
          message.role === "assistant" &&
          message.run_id === runId &&
          message.status === "completed",
      )
    ) {
      return { found: true, pages };
    }
    if (
      page.items.some(
        (message) => message.id === triggerMessageId,
      )
    ) {
      return { found: false, pages };
    }
    const next = page.next_before_sequence;
    if (
      next === null ||
      (beforeSequence !== undefined && next >= beforeSequence)
    ) {
      return { found: false, pages };
    }
    beforeSequence = next;
  }
}

async function findFailedReportMessageId(
  sessionId: string,
  runId: string,
  knownEvents: RunEvent[],
): Promise<string | null> {
  const knownReportMessageId = failedEventReportMessageId(
    knownEvents,
    runId,
  );
  if (knownReportMessageId) {
    return knownReportMessageId;
  }

  let afterSequence = 0;
  while (true) {
    const page = await listRunEvents(sessionId, runId, afterSequence, 100);
    const reportMessageId = failedEventReportMessageId(page.items, runId);
    if (reportMessageId) {
      return reportMessageId;
    }
    const next = page.next_after_sequence;
    // 事件游标不前进时明确失败，不能转为猜测其他 assistant 消息。
    if (next === null || next <= afterSequence) {
      return null;
    }
    afterSequence = next;
  }
}

async function failedReportMessageExists(
  sessionId: string,
  runId: string,
  reportMessageId: string,
  triggerMessageId: string,
): Promise<{
  found: boolean;
  pages: {
    page: ConversationMessagePage;
    beforeSequence: number | undefined;
  }[];
}> {
  let beforeSequence: number | undefined;
  let reportSequence: number | null = null;
  let triggerSequence: number | null = null;
  const pages: {
    page: ConversationMessagePage;
    beforeSequence: number | undefined;
  }[] = [];

  while (true) {
    const page = await listMessages(sessionId, beforeSequence, 20);
    pages.push({ page, beforeSequence });

    for (const message of page.items) {
      if (message.id === triggerMessageId) {
        triggerSequence = message.sequence;
      }
      if (
        message.id === reportMessageId &&
        message.role === "assistant" &&
        message.run_id === runId &&
        message.status === "completed" &&
        message.reply_to_message_id === triggerMessageId
      ) {
        reportSequence = message.sequence;
      }
    }

    if (reportSequence !== null && triggerSequence !== null) {
      return {
        found: reportSequence > triggerSequence,
        pages,
      };
    }
    // 消息页从新到旧；先遇到触发消息后，更旧页面不可能再提供合法报告。
    if (triggerSequence !== null) {
      return { found: false, pages };
    }

    const next = page.next_before_sequence;
    if (
      next === null ||
      (beforeSequence !== undefined && next >= beforeSequence)
    ) {
      return { found: false, pages };
    }
    beforeSequence = next;
  }
}

export async function reconcileCompletedRun(
  sessionId: string,
  runId: string,
  triggerMessageId: string,
): Promise<CompletedRunReconciliation> {
  // completed 事件携带权威消息 ID；事件或消息任一游标耗尽都明确失败。
  const assistantMessageId = await findCompletedAssistantMessageId(
    sessionId,
    runId,
  );
  if (!assistantMessageId) {
    return { kind: "not_found", reason: "completed_event" };
  }
  const finalMessage = await finalAssistantMessageExists(
    sessionId,
    runId,
    assistantMessageId,
    triggerMessageId,
  );
  return finalMessage.found
    ? { kind: "settled", messages: finalMessage.pages }
    : { kind: "not_found", reason: "final_message" };
}

export async function reconcileFailedRun(
  sessionId: string,
  runId: string,
  triggerMessageId: string,
  projectedReportMessageId: string | null,
  knownEvents: RunEvent[] = [],
): Promise<FailedRunReconciliation> {
  const reportMessageId =
    projectedReportMessageId ??
    (await findFailedReportMessageId(sessionId, runId, knownEvents));
  if (!reportMessageId) {
    return { kind: "not_found", reason: "failed_event" };
  }
  const reportMessage = await failedReportMessageExists(
    sessionId,
    runId,
    reportMessageId,
    triggerMessageId,
  );
  return reportMessage.found
    ? { kind: "settled", messages: reportMessage.pages }
    : { kind: "not_found", reason: "report_message" };
}

