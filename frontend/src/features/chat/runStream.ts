import {
  isTerminalRunStatus,
  type RunEvent,
  type RunSnapshotPayload,
  type RunStatus,
  type RunStreamMessage,
} from "./model";

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
  lastHeartbeatAt: string | null;
  messages: RunStreamMessage[];
  notice: string | null;
  runId: string | null;
  status: RunStatus | null;
  transientText: string;
}

export type RunStreamAction =
  | { type: "start"; runId: string; status: RunStatus | null }
  | { type: "sync_status"; runId: string; status: RunStatus }
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
  lastHeartbeatAt: null,
  messages: [],
  notice: null,
  runId: null,
  status: null,
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
        status: eventStatus(action.event.type) ?? state.status,
      };
    }
    case "snapshot": {
      const authoritativeStatus = action.payload.status;
      return {
        ...state,
        connection:
          authoritativeStatus === "waiting_for_input" ||
          authoritativeStatus === "waiting_for_approval" ||
          isTerminalRunStatus(authoritativeStatus)
            ? "closed"
            : "open",
        events: action.payload.events,
        messages: action.payload.messages,
        // 快照是 PostgreSQL 权威结果，必须丢弃此前可能不完整的临时增量。
        transientText: "",
        notice: null,
        status: authoritativeStatus,
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

