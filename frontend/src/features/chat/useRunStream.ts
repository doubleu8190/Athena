import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
} from "react";

import { ApiError, apiRequest } from "../../shared/api/client";
import { runStreamUrl } from "./api";
import {
  isTerminalRunStatus,
  type PendingSubagentApproval,
  type PendingSubagentInput,
  type RunEvent,
  type RunSnapshotPayload,
  type RunStatus,
  type SubagentTaskSummary,
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
export type RunStreamAuthProbe = (signal: AbortSignal) => Promise<void>;

// SSE 断流后发起轻量身份探测的冷却时间，避免断网重连风暴重复刷 /auth/me。
export const AUTH_PROBE_COOLDOWN_MS = 3_000;

const defaultEventSourceFactory: RunEventSourceFactory = (url) =>
  new EventSource(url, { withCredentials: true }) as unknown as RunEventSource;

const defaultProbeAuth: RunStreamAuthProbe = async (signal) => {
  // 复用共享 apiRequest 以保持 authIdentityEpoch 校验与 AUTH_UNAUTHORIZED_EVENT 派发一致。
  await apiRequest<unknown>("/auth/me", {
    method: "GET",
    cache: "no-store",
    signal,
  });
};

interface UseRunStreamOptions {
  enabled?: boolean;
  eventSourceFactory?: RunEventSourceFactory;
  onReconcile?: () => void;
  onSnapshot?: (snapshot: RunSnapshotPayload) => void;
  probeAuth?: RunStreamAuthProbe;
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

function dispatchWireEvent(
  eventName: string,
  data: Record<string, unknown>,
  dispatch: Dispatch<RunStreamAction>,
  onSnapshot?: (snapshot: RunSnapshotPayload) => void,
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
    onSnapshot?.(snapshot);
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
  onSnapshot,
  probeAuth = defaultProbeAuth,
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
  const probeControllerRef = useRef<AbortController | null>(null);
  const reconcileRef = useRef(onReconcile);
  const snapshotRef = useRef(onSnapshot);
  const probeAuthRef = useRef(probeAuth);
  // 缓存最新 status 供建连初始化读取，避免 queued -> running 等正常流转触发 SSE 断流重连。
  const statusRef = useRef(status);
  const mode = runStreamMode(status);
  const alreadyTerminalInStream =
    Boolean(runId) &&
    state.runId === runId &&
    isTerminalRunStatus(state.status);
  // 等待和终态由查询/快照承载；同一 Run 已在实时流中确认终态后，禁止迟到的非终态查询重新拉起 SSE 并覆盖终态。
  const shouldConnect = Boolean(
    enabled && runId && mode === "live" && !alreadyTerminalInStream,
  );
  const streamUrl = useMemo(
    () => (runId ? runStreamUrl(sessionId, runId) : ""),
    [runId, sessionId],
  );

  useEffect(() => {
    reconcileRef.current = onReconcile;
  }, [onReconcile]);

  useEffect(() => {
    snapshotRef.current = onSnapshot;
  }, [onSnapshot]);

  useEffect(() => {
    probeAuthRef.current = probeAuth;
  }, [probeAuth]);

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

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
      // 终态关闭实时流时同步中止在途身份探测，避免旧连接回调继续操作已结束的 Run。
      probeControllerRef.current?.abort();
      probeControllerRef.current = null;
      if (sourceRef.current) {
        sourceRef.current.onopen = null;
        sourceRef.current.onerror = null;
        sourceRef.current.close();
        sourceRef.current = null;
      }
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

    dispatch({ runId, status: statusRef.current, type: "start" });
    let acceptEvents = true;
    let snapshotReceived = false;
    // 为本次连接生命周期绑定独立 AbortController，卸载、切 Run、终态关闭或换线时统一取消探测。
    const probeController = new AbortController();
    probeControllerRef.current = probeController;
    let probeInFlight = false;
    let lastProbeFinishedAt: number | null = null;
    let openGeneration = 0;

    let source: RunEventSource;
    try {
      source = eventSourceFactory(streamUrl);
    } catch {
      probeController.abort();
      if (probeControllerRef.current === probeController) {
        probeControllerRef.current = null;
      }
      dispatch({ type: "error" });
      return;
    }
    sourceRef.current = source;

    const closeSource = (abortProbe: boolean) => {
      acceptEvents = false;
      if (abortProbe) {
        probeController.abort();
        if (probeControllerRef.current === probeController) {
          probeControllerRef.current = null;
        }
      }
      source.onopen = null;
      source.onerror = null;
      source.close();
      if (sourceRef.current === source) {
        sourceRef.current = null;
      }
    };

    const probeAuthentication = async () => {
      if (probeInFlight) {
        return;
      }
      if (
        lastProbeFinishedAt !== null &&
        Date.now() - lastProbeFinishedAt < AUTH_PROBE_COOLDOWN_MS
      ) {
        return;
      }
      probeInFlight = true;
      const probeOpenGeneration = openGeneration;
      try {
        await probeAuthRef.current(probeController.signal);
      } catch (error) {
        if (
          acceptEvents &&
          !probeController.signal.aborted &&
          error instanceof ApiError &&
          error.status === 401
        ) {
          // 探测确认 401 时，apiRequest 已派发全局未认证事件；立刻关闭 SSE 阻止浏览器自动重连。
          closeSource(false);
        }
        // 其他错误（瞬态网络故障、5xx 或生命周期中止）均静默吞掉，保留断线提示与当前文本。
      } finally {
        probeInFlight = false;
        if (
          !probeController.signal.aborted &&
          probeOpenGeneration === openGeneration
        ) {
          lastProbeFinishedAt = Date.now();
        }
      }
    };

    const handleMessage = (
      eventName: string,
      event: MessageEvent<string>,
    ) => {
      if (!acceptEvents || probeController.signal.aborted) {
        return;
      }
      const data = parseEventData(event.data);
      if (!data) {
        dispatch({ type: "error" });
        return;
      }
      const outcome = dispatchWireEvent(
        eventName,
        data,
        dispatch,
        snapshotRef.current,
      );
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
        // 服务端在这些状态会主动结束流；本地立刻关闭并中止探测，阻止 EventSource 自动重连。
        closeSource(true);
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
        // 权威 snapshot。旧连接立即关闭并取消探测，本地 guard 防止同一代次重复触发。
        closeSource(true);
        setReconnectGeneration((generation) => generation + 1);
      }
    };

    source.onopen = () => {
      if (!acceptEvents || probeController.signal.aborted) {
        return;
      }
      // 连接恢复后重置冷却窗口，确保后续若再次会话过期可立即探测。
      openGeneration += 1;
      lastProbeFinishedAt = null;
      dispatch({ type: "open" });
    };
    source.onerror = () => {
      if (!acceptEvents || probeController.signal.aborted) {
        return;
      }
      dispatch({ type: "error" });
      // 原生 EventSource 不暴露 HTTP 状态码，断流后通过轻量身份探测识别 401 会话过期。
      void probeAuthentication();
    };
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
      closeSource(true);
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

