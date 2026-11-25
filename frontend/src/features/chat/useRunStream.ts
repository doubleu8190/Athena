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

export interface RunStreamReconcileContext {
  eventType?: RunEvent["type"];
  forceMessages?: boolean;
}

interface UseRunStreamOptions {
  enabled?: boolean;
  eventSourceFactory?: RunEventSourceFactory;
  onReconcile?: (context?: RunStreamReconcileContext) => void;
  onSnapshot?: (snapshot: RunSnapshotPayload) => void;
  probeAuth?: RunStreamAuthProbe;
  runId: string | null;
  sessionId: string;
  status: RunStatus | null;
}

interface UseRunStreamResult {
  closeTerminal: (runId: string, status: RunStatus) => void;
  /** 最近一次 SSE 心跳时间戳（仅保活观测使用，存放在 ref 避免每 15s 触发整页重渲染）。 */
  lastHeartbeatAtRef: RefObject<string | null>;
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

type WireEventOutcome =
  | { kind: "snapshot_waiting" }
  | { kind: "snapshot_terminal" }
  | { kind: "snapshot_live" }
  | { eventType?: RunEvent["type"]; kind: "run_event" }
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
    return { eventType: event?.type, kind: "run_event" };
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
    probeAuthRef.current = probeAuth;
  }, [probeAuth]);

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
      cancelScheduledTextFlush();
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
          flushPendingText();
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
        flushPendingText();
        dispatch({ type: "error" });
        return;
      }
      const outcome = dispatchWireEvent(eventName, data, dispatch, {
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
        reconcileRef.current?.({ eventType: outcome.eventType });
      }
      if (
        outcome.kind === "snapshot_terminal" ||
        outcome.kind === "snapshot_waiting"
      ) {
        // 服务端在这些状态会主动结束流；本地立刻关闭并中止探测，阻止 EventSource 自动重连。
        closeSource(true);
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
      // 断线前先刷出缓冲草稿，保证用户看到的断线保留文本不缺失最后 1 帧。
      flushPendingText();
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
      // 仅当同一 Run 仍处于挂载态收尾关流（例如查询先确认终态）时同步 flush 缓冲；
      // 切 Run 或组件卸载时直接丢弃缓冲并取消挂起帧，避免跨 Run 串染或卸载后派发。
      if (mountedRef.current && latestRunIdRef.current === runId) {
        flushPendingText();
      } else {
        discardPendingText();
      }
      closeSource(true);
    };
  }, [
    cancelScheduledTextFlush,
    discardPendingText,
    enqueueTextDelta,
    eventSourceFactory,
    flushPendingText,
    mode,
    runId,
    shouldConnect,
    reconnectGeneration,
    streamUrl,
  ]);

  return {
    closeTerminal,
    lastHeartbeatAtRef,
    settleTerminal,
    source: sourceRef.current,
    state,
    syncStatus,
  };
}

