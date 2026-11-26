import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
} from "react";

import {
  connectEventStream,
  type ConnectEventStreamOptions,
  type EventStreamConnection,
  type SseFetch,
  type SseMessage,
} from "../../shared/api/sse";
import type { ArtifactFileRef } from "../files/model";
import { runStreamUrl } from "./api";
import {
  isTerminalRunStatus,
  type ConversationMessagePart,
  type PendingSubagentApproval,
  type PendingSubagentInput,
  type RunEvent,
  type RunSnapshotPayload,
  type RunStatus,
  type RunStreamMessage,
  type SubagentTaskSummary,
} from "./model";
import {
  INITIAL_RUN_STREAM_STATE,
  runStreamMode,
  runStreamReducer,
  type RunStreamAction,
  type RunStreamState,
} from "./runStream";

export type RunStreamConnector = (
  url: string,
  options: ConnectEventStreamOptions,
) => EventStreamConnection;

export interface RunStreamReconcileContext {
  eventType?: RunEvent["type"];
  forceMessages?: boolean;
}

interface UseRunStreamOptions {
  enabled?: boolean;
  /** 可选自定义 fetch，供单测或特殊环境注入。 */
  fetch?: SseFetch;
  onReconcile?: (context?: RunStreamReconcileContext) => void;
  onSnapshot?: (snapshot: RunSnapshotPayload) => void;
  runId: string | null;
  sessionId: string;
  status: RunStatus | null;
  /** 可选自定义 SSE 连接器工厂，默认使用基于 fetch + eventsource-parser 的 connectEventStream。 */
  streamConnector?: RunStreamConnector;
}

interface UseRunStreamResult {
  closeTerminal: (runId: string, status: RunStatus) => void;
  /** 当前活跃 SSE 连接句柄（只读观测使用）。 */
  connection: EventStreamConnection | null;
  /** 最近一次 SSE 心跳时间戳（仅保活观测使用，存放在 ref 避免每 15s 触发整页重渲染）。 */
  lastHeartbeatAtRef: RefObject<string | null>;
  settleTerminal: (runId: string, status: RunStatus) => void;
  state: RunStreamState;
  syncStatus: (runId: string, status: RunStatus) => void;
}

const SUPPORTED_RUN_STREAM_EVENTS = new Set([
  "snapshot",
  "text_delta",
  "run_event",
  "heartbeat",
  "stream_interrupted",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseEventData(raw: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function parseArtifactFileRef(value: unknown): ArtifactFileRef | null {
  if (
    !isRecord(value) ||
    typeof value.file_id !== "string" ||
    typeof value.filename !== "string" ||
    typeof value.mime_type !== "string" ||
    typeof value.size_bytes !== "number"
  ) {
    return null;
  }
  return {
    file_id: value.file_id,
    filename: value.filename,
    label: typeof value.label === "string" ? value.label : null,
    mime_type: value.mime_type,
    size_bytes: value.size_bytes,
  };
}

function parseMessagePart(value: unknown): ConversationMessagePart | null {
  if (!isRecord(value) || typeof value.type !== "string") {
    return null;
  }
  if (value.type === "text" && typeof value.text === "string") {
    return { text: value.text, type: "text" };
  }
  if (
    value.type === "file_ref" &&
    typeof value.file_id === "string" &&
    typeof value.filename === "string"
  ) {
    return {
      file_id: value.file_id,
      filename: value.filename,
      mime_type: typeof value.mime_type === "string" ? value.mime_type : null,
      size_bytes: typeof value.size_bytes === "number" ? value.size_bytes : null,
      type: "file_ref",
    };
  }
  if (
    value.type === "tool_call" &&
    typeof value.tool_call_id === "string" &&
    typeof value.name === "string"
  ) {
    return {
      arguments: isRecord(value.arguments) ? value.arguments : {},
      name: value.name,
      tool_call_id: value.tool_call_id,
      type: "tool_call",
    };
  }
  if (
    value.type === "tool_result" &&
    typeof value.tool_call_id === "string" &&
    typeof value.status === "string"
  ) {
    const artifacts: ArtifactFileRef[] = [];
    if (Array.isArray(value.artifacts)) {
      for (const rawArtifact of value.artifacts) {
        const parsedArtifact = parseArtifactFileRef(rawArtifact);
        if (!parsedArtifact) {
          return null;
        }
        artifacts.push(parsedArtifact);
      }
    }
    return {
      artifact_id:
        typeof value.artifact_id === "string" ? value.artifact_id : null,
      artifacts,
      output: typeof value.output === "string" ? value.output : null,
      status: value.status,
      tool_call_id: value.tool_call_id,
      type: "tool_result",
    };
  }
  return null;
}

function parseRunStreamMessage(value: unknown): RunStreamMessage | null {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    (value.role !== "user" && value.role !== "assistant") ||
    typeof value.status !== "string" ||
    typeof value.text !== "string" ||
    typeof value.sequence !== "number" ||
    typeof value.created_at !== "string"
  ) {
    return null;
  }
  const message: RunStreamMessage = {
    created_at: value.created_at,
    id: value.id,
    role: value.role,
    sequence: value.sequence,
    status: value.status,
    text: value.text,
  };
  if (typeof value.session_id === "string" || value.session_id === null) {
    message.session_id = value.session_id;
  }
  if (typeof value.run_id === "string" || value.run_id === null) {
    message.run_id = value.run_id;
  }
  if (
    typeof value.reply_to_message_id === "string" ||
    value.reply_to_message_id === null
  ) {
    message.reply_to_message_id = value.reply_to_message_id;
  }
  if (typeof value.completed_at === "string" || value.completed_at === null) {
    message.completed_at = value.completed_at;
  }
  if (Array.isArray(value.parts)) {
    const parsedParts: ConversationMessagePart[] = [];
    for (const rawPart of value.parts) {
      const parsedPart = parseMessagePart(rawPart);
      if (!parsedPart) {
        // 存在未知或格式异常的内容块时放弃内联 parts，交由单次有界 /messages 兜底同步避免静默丢块。
        return message;
      }
      parsedParts.push(parsedPart);
    }
    message.parts = parsedParts;
  } else if (value.parts === null) {
    message.parts = null;
  }
  return message;
}

export function parseSnapshotPayload(value: unknown): RunSnapshotPayload | null {
  if (!isRecord(value) || typeof value.status !== "string") {
    return null;
  }
  const status = value.status as RunStatus;
  const messages: RunStreamMessage[] = [];
  if (Array.isArray(value.messages)) {
    for (const rawMessage of value.messages) {
      const parsed = parseRunStreamMessage(rawMessage);
      if (parsed) {
        messages.push(parsed);
      }
    }
  }
  return {
    assistant_message_id:
      typeof value.assistant_message_id === "string" &&
      value.assistant_message_id
        ? value.assistant_message_id
        : null,
    events: Array.isArray(value.events)
      ? (value.events as RunEvent[])
      : [],
    events_after_sequence:
      typeof value.events_after_sequence === "number"
        ? value.events_after_sequence
        : null,
    events_complete: Boolean(value.events_complete),
    event_sequence:
      typeof value.event_sequence === "number" ? value.event_sequence : 0,
    messages,
    messages_before_sequence:
      typeof value.messages_before_sequence === "number"
        ? value.messages_before_sequence
        : null,
    messages_complete: Boolean(value.messages_complete),
    report_message_id:
      typeof value.report_message_id === "string" &&
      value.report_message_id
        ? value.report_message_id
        : null,
    status,
    subagent_tasks: Array.isArray(value.subagent_tasks)
      ? (value.subagent_tasks as SubagentTaskSummary[])
      : [],
    pending_subagent_inputs: Array.isArray(value.pending_subagent_inputs)
      ? (value.pending_subagent_inputs as PendingSubagentInput[])
      : [],
    pending_subagent_approvals: Array.isArray(value.pending_subagent_approvals)
      ? (value.pending_subagent_approvals as PendingSubagentApproval[])
      : [],
  };
}

function parseRunEvent(value: unknown): RunEvent | null {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    typeof value.run_id !== "string" ||
    typeof value.sequence !== "number" ||
    typeof value.type !== "string" ||
    typeof value.created_at !== "string" ||
    !isRecord(value.payload)
  ) {
    return null;
  }
  return value as unknown as RunEvent;
}

type WireEventOutcome =
  | { kind: "snapshot_waiting" }
  | { kind: "snapshot_terminal" }
  | { kind: "snapshot_live" }
  | { eventRunId?: string; eventType?: RunEvent["type"]; kind: "run_event" }
  | { kind: "interrupted" }
  | null;

interface DispatchWireEventHandlers {
  enqueueTextDelta: (text: string) => void;
  flushPendingText: () => void;
  onHeartbeat: (at: string) => void;
  onSnapshot?: (snapshot: RunSnapshotPayload) => void;
}

function dispatchWireEvent(
  eventName: string,
  data: Record<string, unknown>,
  dispatch: Dispatch<RunStreamAction>,
  handlers: DispatchWireEventHandlers,
): WireEventOutcome {
  const payload = data.payload;
  if (eventName === "snapshot") {
    const snapshot = parseSnapshotPayload(payload);
    if (!snapshot) {
      return null;
    }
    // 权威快照落地前先同步清空本帧文本缓冲并取消挂起帧：
    // - 终态快照会保留缓冲草稿直至最终消息对账完成；
    // - 非终态快照随后以权威消息覆盖清空 transientText，避免旧帧迟到回写。
    handlers.flushPendingText();
    dispatch({ payload: snapshot, type: "snapshot" });
    handlers.onSnapshot?.(snapshot);
    if (
      snapshot.status === "waiting_for_input" ||
      snapshot.status === "waiting_for_approval"
    ) {
      return { kind: "snapshot_waiting" };
    }
    if (
      snapshot.status === "completed" ||
      snapshot.status === "failed" ||
      snapshot.status === "cancelled"
    ) {
      return { kind: "snapshot_terminal" };
    }
    return { kind: "snapshot_live" };
  }
  if (eventName === "text_delta") {
    if (isRecord(payload) && typeof payload.text === "string" && payload.text) {
      handlers.enqueueTextDelta(payload.text);
    }
    return null;
  }
  if (eventName === "stream_interrupted") {
    // 断流提示前先刷出已收到的增量文本，防止最后一帧草稿滞留在缓冲中丢失。
    handlers.flushPendingText();
    dispatch({
      reason:
        isRecord(payload) && typeof payload.reason === "string"
          ? payload.reason
          : "stream_gap",
      type: "stream_interrupted",
    });
    return { kind: "interrupted" };
  }
  if (eventName === "heartbeat") {
    // 心跳仅做保活记录，写入 ref 而不派发 React state，消除每 15s 一次的整页重渲染。
    handlers.onHeartbeat(
      isRecord(payload) && typeof payload.at === "string"
        ? payload.at
        : new Date().toISOString(),
    );
    return null;
  }
  if (eventName === "run_event" && isRecord(payload)) {
    const event = parseRunEvent(payload.event);
    // 持久事件（尤其是终态/等待态）前先同步 flush 文本缓冲，严格维持事件顺序。
    handlers.flushPendingText();
    if (event) {
      dispatch({ event, type: "run_event" });
    }
    return { eventRunId: event?.run_id, eventType: event?.type, kind: "run_event" };
  }
  return null;
}

export function useRunStream({
  enabled = true,
  fetch: customFetch,
  onReconcile,
  onSnapshot,
  runId,
  sessionId,
  status,
  streamConnector = connectEventStream,
}: UseRunStreamOptions): UseRunStreamResult {
  const [state, dispatch] = useReducer(
    runStreamReducer,
    INITIAL_RUN_STREAM_STATE,
  );
  const [reconnectGeneration, setReconnectGeneration] = useState(0);
  const connectionRef = useRef<EventStreamConnection | null>(null);
  const reconcileRef = useRef(onReconcile);
  const snapshotRef = useRef(onSnapshot);
  // 缓存最新 status 供建连初始化读取，避免 queued -> running 等正常流转触发 SSE 断流重连。
  const statusRef = useRef(status);
  // 同步记录最新 runId 与挂载状态，便于连接清理时精确区分「同 Run 终态收尾」与「切 Run / 卸载」。
  const latestRunIdRef = useRef(runId);
  latestRunIdRef.current = runId;
  const activeRunIdRef = useRef<string | null>(null);
  const mountedRef = useRef(true);
  const lastHeartbeatAtRef = useRef<string | null>(null);
  // 同帧高频 text_delta 合批缓冲：同一渲染帧内多次增量仅合并为 1 次 dispatch。
  const pendingTextRef = useRef("");
  const textFlushHandleRef = useRef<{
    id: number;
    kind: "raf" | "timeout";
  } | null>(null);
  const mode = runStreamMode(status);
  // 仅当同一 Run 已收到终态权威 snapshot，或流在终态下已关闭/出错时才阻断/断开 SSE；
  // 若仅由终态 run_event 预告终态且连接仍打开，必须保持连接接收服务端下一帧发出的自包含终态 snapshot。
  const alreadyTerminalInStream =
    Boolean(runId) &&
    state.runId === runId &&
    isTerminalRunStatus(state.status) &&
    (state.terminalSnapshotReceived ||
      state.connection === "closed" ||
      state.connection === "error");
  // 等待和终态由查询/快照承载；同一 Run 已在实时流中终态收尾后，禁止迟到的非终态查询重新拉起 SSE 并覆盖终态。
  const shouldConnect = Boolean(
    enabled && runId && mode === "live" && !alreadyTerminalInStream,
  );
  const streamUrl = useMemo(
    () => (runId ? runStreamUrl(sessionId, runId) : ""),
    [runId, sessionId],
  );

  // 声明在连接 effect 之前：卸载时先置 mountedRef = false，确保连接清理走丢弃缓冲路径而非派发更新。
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    reconcileRef.current = onReconcile;
  }, [onReconcile]);

  useEffect(() => {
    snapshotRef.current = onSnapshot;
  }, [onSnapshot]);

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  const cancelScheduledTextFlush = useCallback(() => {
    const handle = textFlushHandleRef.current;
    if (!handle) {
      return;
    }
    textFlushHandleRef.current = null;
    if (
      handle.kind === "raf" &&
      typeof window !== "undefined" &&
      typeof window.cancelAnimationFrame === "function"
    ) {
      window.cancelAnimationFrame(handle.id);
      return;
    }
    globalThis.clearTimeout(handle.id);
  }, []);

  const discardPendingText = useCallback(() => {
    cancelScheduledTextFlush();
    pendingTextRef.current = "";
  }, [cancelScheduledTextFlush]);

  const flushPendingText = useCallback(() => {
    cancelScheduledTextFlush();
    const buffered = pendingTextRef.current;
    if (!buffered) {
      return;
    }
    pendingTextRef.current = "";
    dispatch({ text: buffered, type: "text_delta" });
  }, [cancelScheduledTextFlush]);

  const enqueueTextDelta = useCallback((text: string) => {
    pendingTextRef.current += text;
    if (textFlushHandleRef.current !== null) {
      return;
    }
    const flushFrame = () => {
      textFlushHandleRef.current = null;
      const buffered = pendingTextRef.current;
      if (!buffered) {
        return;
      }
      pendingTextRef.current = "";
      dispatch({ text: buffered, type: "text_delta" });
    };
    if (
      typeof window !== "undefined" &&
      typeof window.requestAnimationFrame === "function"
    ) {
      const id = window.requestAnimationFrame(flushFrame);
      textFlushHandleRef.current = { id, kind: "raf" };
      return;
    }
    // 非浏览器或无 rAF 环境回退至 16ms 定时合批。
    const id = globalThis.setTimeout(flushFrame, 16) as unknown as number;
    textFlushHandleRef.current = { id, kind: "timeout" };
  }, []);

  const syncStatus = useCallback(
    (targetRunId: string, nextStatus: RunStatus) => {
      if (activeRunIdRef.current !== targetRunId) {
        discardPendingText();
      } else if (isTerminalRunStatus(nextStatus)) {
        flushPendingText();
      }
      activeRunIdRef.current = targetRunId;
      dispatch({
        runId: targetRunId,
        status: nextStatus,
        type: "sync_status",
      });
    },
    [discardPendingText, flushPendingText],
  );

  const closeTerminal = useCallback(
    (targetRunId: string, nextStatus: RunStatus) => {
      // 查询确认同一 Run 进入终态时，先同步刷出最后一帧缓冲草稿，避免对账完成前丢失未落盘文本。
      if (activeRunIdRef.current === targetRunId) {
        flushPendingText();
      } else {
        discardPendingText();
      }
      if (connectionRef.current) {
        connectionRef.current.close();
        connectionRef.current = null;
      }
      dispatch({
        runId: targetRunId,
        status: nextStatus,
        type: "query_terminal",
      });
    },
    [discardPendingText, flushPendingText],
  );

  const settleTerminal = useCallback(
    (targetRunId: string, nextStatus: RunStatus) => {
      // 终态权威消息已对账收敛，立刻清理任何残留文本缓冲与挂起帧，避免延迟回写旧草稿。
      if (activeRunIdRef.current === targetRunId) {
        discardPendingText();
      }
      dispatch({
        runId: targetRunId,
        status: nextStatus,
        type: "settle_terminal",
      });
    },
    [discardPendingText],
  );

  useEffect(() => {
    if (!runId || !status || !isTerminalRunStatus(status)) {
      return;
    }
    // 查询已确认终态时先停止实时流；临时文本等待持久消息对账成功后再收敛。
    closeTerminal(runId, status);
  }, [closeTerminal, runId, status]);

  useEffect(() => {
    if (!shouldConnect || !runId) {
      return;
    }

    if (activeRunIdRef.current !== runId) {
      discardPendingText();
      lastHeartbeatAtRef.current = null;
    }
    activeRunIdRef.current = runId;
    dispatch({ runId, status: statusRef.current, type: "start" });

    let acceptEvents = true;
    let snapshotReceived = false;
    let terminalEventSeen = false;
    let connection: EventStreamConnection | null = null;

    const closeConnection = () => {
      acceptEvents = false;
      cancelScheduledTextFlush();
      connection?.close();
      if (connection && connectionRef.current === connection) {
        connectionRef.current = null;
      }
    };

    const handleEvent = (message: SseMessage) => {
      if (!acceptEvents || !SUPPORTED_RUN_STREAM_EVENTS.has(message.event)) {
        return;
      }
      const data = parseEventData(message.data);
      if (!data) {
        flushPendingText();
        dispatch({ type: "error" });
        return;
      }
      const outcome = dispatchWireEvent(message.event, data, dispatch, {
        enqueueTextDelta,
        flushPendingText,
        onHeartbeat: (at) => {
          lastHeartbeatAtRef.current = at;
        },
        onSnapshot: snapshotRef.current,
      });
      if (!outcome) {
        return;
      }
      if (outcome.kind === "snapshot_live") {
        snapshotReceived = true;
      }
      if (outcome.kind === "run_event") {
        if (
          outcome.eventRunId === runId &&
          (outcome.eventType === "completed" ||
            outcome.eventType === "failed" ||
            outcome.eventType === "cancelled")
        ) {
          terminalEventSeen = true;
        }
        reconcileRef.current?.({ eventType: outcome.eventType });
      }
      if (
        outcome.kind === "snapshot_terminal" ||
        outcome.kind === "snapshot_waiting"
      ) {
        // 服务端在终态/等待态会主动结束流；本地立刻关流阻止退避重连，并触发强制消息对账。
        closeConnection();
        dispatch({ type: "closed" });
        reconcileRef.current?.({ forceMessages: true });
        return;
      }
      if (outcome.kind === "interrupted") {
        if (!snapshotReceived) {
          // 新连接尚未收到首个权威快照时，服务端会继续发送 snapshot；
          // 此处不再次换线，避免持续中断信号导致重连风暴。
          return;
        }
        // 服务端在同代次内不会重复快照；收到中断后关闭旧流并触发全新连接（无 Last-Event-ID）拉取权威 snapshot。
        closeConnection();
        setReconnectGeneration((generation) => generation + 1);
      }
    };

    try {
      connection = streamConnector(streamUrl, {
        fetch: customFetch,
        onError: (errorInfo) => {
          if (!acceptEvents) {
            return;
          }
          // 已收到当前 Run 终态 run_event 预告后若服务端直接 EOF 结束流（无终态 snapshot），
          // 立即安静关流阻止 sse.ts 误判断线重连，并交由控制器接管单次有界兜底。
          if (terminalEventSeen && errorInfo.reason === "stream_closed") {
            flushPendingText();
            closeConnection();
            dispatch({ type: "closed" });
            return;
          }
          // 断线或 401/403/404 停流前先刷出缓冲草稿，保证用户看到的保留文本不缺失最后 1 帧。
          flushPendingText();
          dispatch({ type: "error" });
          if (errorInfo.fatal || terminalEventSeen) {
            // 401/403/404 等不可恢复错误或终态事件后的断流永久停流，本地同步断开句柄。
            closeConnection();
          }
        },
        onEvent: handleEvent,
        onOpen: () => {
          if (!acceptEvents) {
            return;
          }
          dispatch({ type: "open" });
        },
      });
    } catch {
      dispatch({ type: "error" });
      return;
    }
    if (!acceptEvents) {
      connection.close();
      return;
    }
    connectionRef.current = connection;

    return () => {
      // 仅当同一 Run 仍处于挂载态收尾关流（例如查询先确认终态）时同步 flush 缓冲；
      // 切 Run 或组件卸载时直接丢弃缓冲并取消挂起帧，避免跨 Run 串染或卸载后派发。
      if (mountedRef.current && latestRunIdRef.current === runId) {
        flushPendingText();
      } else {
        discardPendingText();
      }
      closeConnection();
    };
  }, [
    cancelScheduledTextFlush,
    customFetch,
    discardPendingText,
    enqueueTextDelta,
    flushPendingText,
    mode,
    runId,
    shouldConnect,
    reconnectGeneration,
    streamConnector,
    streamUrl,
  ]);

  return {
    closeTerminal,
    connection: connectionRef.current,
    lastHeartbeatAtRef,
    settleTerminal,
    state,
    syncStatus,
  };
}
