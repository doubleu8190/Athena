import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
} from "react";

import { runStreamUrl } from "./api";
import {
  isTerminalRunStatus,
  type RunEvent,
  type RunSnapshotPayload,
  type RunStatus,
} from "./model";
import {
  INITIAL_RUN_STREAM_STATE,
  runStreamMode,
  runStreamReducer,
  type RunStreamAction,
  type RunStreamState,
} from "./runStream";

export interface RunEventSource {
  addEventListener(
    type: string,
    listener: (event: MessageEvent<string>) => void,
  ): void;
  close(): void;
  onerror: ((event: Event) => void) | null;
  onopen: ((event: Event) => void) | null;
}

export type RunEventSourceFactory = (url: string) => RunEventSource;

const defaultEventSourceFactory: RunEventSourceFactory = (url) =>
  new EventSource(url, { withCredentials: true }) as unknown as RunEventSource;

interface UseRunStreamOptions {
  enabled?: boolean;
  eventSourceFactory?: RunEventSourceFactory;
  onReconcile?: () => void;
  runId: string | null;
  sessionId: string;
  status: RunStatus | null;
}

interface UseRunStreamResult {
  closeTerminal: (runId: string, status: RunStatus) => void;
  settleTerminal: (runId: string, status: RunStatus) => void;
  source: RunEventSource | null;
  state: RunStreamState;
  syncStatus: (runId: string, status: RunStatus) => void;
}

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

function parseSnapshotPayload(value: unknown): RunSnapshotPayload | null {
  if (!isRecord(value) || typeof value.status !== "string") {
    return null;
  }
  const status = value.status as RunStatus;
  const messages = Array.isArray(value.messages)
    ? value.messages.filter(
        (message): message is RunSnapshotPayload["messages"][number] => {
          if (
            !isRecord(message) ||
            typeof message.id !== "string" ||
            (message.role !== "user" && message.role !== "assistant") ||
            typeof message.status !== "string" ||
            typeof message.text !== "string" ||
            typeof message.sequence !== "number" ||
            typeof message.created_at !== "string"
          ) {
            return false;
          }
          return true;
        },
      )
    : [];
  return {
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
    status,
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

function dispatchWireEvent(
  eventName: string,
  data: Record<string, unknown>,
  dispatch: Dispatch<RunStreamAction>,
):
  | "snapshot_waiting"
  | "snapshot_terminal"
  | "snapshot_live"
  | "run_event"
  | "interrupted"
  | null {
  const payload = data.payload;
  if (eventName === "snapshot") {
    const snapshot = parseSnapshotPayload(payload);
    if (!snapshot) {
      return null;
    }
    dispatch({ payload: snapshot, type: "snapshot" });
    if (
      snapshot.status === "waiting_for_input" ||
      snapshot.status === "waiting_for_approval"
    ) {
      return "snapshot_waiting";
    }
    if (
      snapshot.status === "completed" ||
      snapshot.status === "failed" ||
      snapshot.status === "cancelled"
    ) {
      return "snapshot_terminal";
    }
    return "snapshot_live";
  }
  if (eventName === "text_delta") {
    if (isRecord(payload) && typeof payload.text === "string") {
      dispatch({ text: payload.text, type: "text_delta" });
    }
    return null;
  }
  if (eventName === "stream_interrupted") {
    dispatch({
      reason:
        isRecord(payload) && typeof payload.reason === "string"
          ? payload.reason
          : "stream_gap",
      type: "stream_interrupted",
    });
    return "interrupted";
  }
  if (eventName === "heartbeat") {
    dispatch({
      at:
        isRecord(payload) && typeof payload.at === "string"
          ? payload.at
          : new Date().toISOString(),
      type: "heartbeat",
    });
    return null;
  }
  if (eventName === "run_event" && isRecord(payload)) {
    const event = parseRunEvent(payload.event);
    if (event) {
      dispatch({ event, type: "run_event" });
    }
    return "run_event";
  }
  return null;
}

export function useRunStream({
  enabled = true,
  eventSourceFactory = defaultEventSourceFactory,
  onReconcile,
  runId,
  sessionId,
  status,
}: UseRunStreamOptions): UseRunStreamResult {
  const [state, dispatch] = useReducer(
    runStreamReducer,
    INITIAL_RUN_STREAM_STATE,
  );
  const [reconnectGeneration, setReconnectGeneration] = useState(0);
  const sourceRef = useRef<RunEventSource | null>(null);
  const reconcileRef = useRef(onReconcile);
  const mode = runStreamMode(status);
  // 等待和终态由查询/快照承载；只有仍可能产生实时增量的状态才建立连接。
  const shouldConnect = Boolean(enabled && runId && mode === "live");
  const streamUrl = useMemo(
    () => (runId ? runStreamUrl(sessionId, runId) : ""),
    [runId, sessionId],
  );

  useEffect(() => {
    reconcileRef.current = onReconcile;
  }, [onReconcile]);

  const syncStatus = useCallback(
    (targetRunId: string, nextStatus: RunStatus) => {
      dispatch({
        runId: targetRunId,
        status: nextStatus,
        type: "sync_status",
      });
    },
    [],
  );

  const closeTerminal = useCallback(
    (targetRunId: string, nextStatus: RunStatus) => {
      sourceRef.current?.close();
      sourceRef.current = null;
      dispatch({
        runId: targetRunId,
        status: nextStatus,
        type: "query_terminal",
      });
    },
    [],
  );

  const settleTerminal = useCallback(
    (targetRunId: string, nextStatus: RunStatus) => {
      dispatch({
        runId: targetRunId,
        status: nextStatus,
        type: "settle_terminal",
      });
    },
    [],
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

    dispatch({ runId, status, type: "start" });
    let acceptEvents = true;
    let snapshotReceived = false;
    let source: RunEventSource;
    try {
      source = eventSourceFactory(streamUrl);
    } catch {
      dispatch({ type: "error" });
      return;
    }
    sourceRef.current = source;

    const handleMessage = (
      eventName: string,
      event: MessageEvent<string>,
    ) => {
      if (!acceptEvents) {
        return;
      }
      const data = parseEventData(event.data);
      if (!data) {
        dispatch({ type: "error" });
        return;
      }
      const outcome = dispatchWireEvent(eventName, data, dispatch);
      if (outcome === "snapshot_live") {
        snapshotReceived = true;
      }
      if (outcome === "run_event") {
        reconcileRef.current?.();
      }
      if (
        outcome === "snapshot_terminal" ||
        outcome === "snapshot_waiting"
      ) {
        // 服务端在这些状态会主动结束流；本地立刻关闭可阻止 EventSource 自动重连。
        acceptEvents = false;
        source.close();
        dispatch({ type: "closed" });
        reconcileRef.current?.();
        return;
      }
      if (outcome === "interrupted") {
        if (!snapshotReceived) {
          // 新连接尚未收到首个权威快照时，服务端会继续发送 snapshot；
          // 此处不再次换线，避免持续中断信号导致重连风暴。
          return;
        }
        // 服务端在同代次内不会重复快照；收到中断后必须用全新连接请求
        // 权威 snapshot。旧连接立即关闭，本地 guard 防止同一代次重复触发。
        acceptEvents = false;
        source.close();
        if (sourceRef.current === source) {
          sourceRef.current = null;
        }
        setReconnectGeneration((generation) => generation + 1);
      }
    };

    source.onopen = () => dispatch({ type: "open" });
    source.onerror = () => dispatch({ type: "error" });
    for (const eventName of [
      "snapshot",
      "text_delta",
      "run_event",
      "heartbeat",
      "stream_interrupted",
    ]) {
      source.addEventListener(eventName, (event) =>
        handleMessage(eventName, event),
      );
    }

    return () => {
      acceptEvents = false;
      source.onopen = null;
      source.onerror = null;
      source.close();
      if (sourceRef.current === source) {
        sourceRef.current = null;
      }
    };
  }, [
    eventSourceFactory,
    mode,
    runId,
    shouldConnect,
    reconnectGeneration,
    streamUrl,
  ]);

  return {
    closeTerminal,
    settleTerminal,
    source: sourceRef.current,
    state,
    syncStatus,
  };
}

