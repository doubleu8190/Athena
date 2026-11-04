import { useEffect, useRef } from "react"
import { useChatStore, type SessionViewState } from "../store/chatStore"
import {
  sessionEventStreamUrl,
  timelineEntriesMatch,
} from "../utils/sessionHistory.ts"
import { SESSION_EVENT_NAMES } from "../utils/sessionEventCatalog.ts"
import type {
  ApplicationEventEnvelope,
  SessionSnapshotPayload,
} from "../types/events"
import type { ExecutionTimelineEntry, Message } from "../types"
import { applySessionSnapshot } from "../utils/sessionSnapshot"

type View = Partial<SessionViewState>

const EMPTY_VIEW: View = {
  messages: [],
  executionTimeline: [],
  agentStatus: "idle",
  error: null,
  errorDetail: null,
}

/** 清除删除或登出会话的内存同步状态。 */
export function clearSessionEventCache(sessionId: string): void {
  useChatStore.setState((state) => {
    const sessionSyncTokens = { ...state.sessionSyncTokens }
    const sessionHistoryLoaded = { ...state.sessionHistoryLoaded }
    const sessionViewCache = { ...state.sessionViewCache }
    delete sessionSyncTokens[sessionId]
    delete sessionHistoryLoaded[sessionId]
    delete sessionViewCache[sessionId]
    return { sessionSyncTokens, sessionHistoryLoaded, sessionViewCache }
  })
}

function text(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function objectValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function progressTime(value: unknown, fallback: string): string {
  return text(value) || fallback
}

function upsertMessage(messages: Message[], incoming: Message): Message[] {
  const index = messages.findIndex((message) => (
    message.id === incoming.id
    || (
      message.role === incoming.role
      && (
        (Boolean(message.run_id) && message.run_id === incoming.run_id)
        || (
          message.timestamp === incoming.timestamp
          && message.content === incoming.content
        )
      )
    )
  ))
  if (index < 0) return [...messages, incoming].sort(compareMessages)
  const existing = messages[index]
  // 完成正文是不可逆终态，不能被重放中的空 started/delta 覆盖。
  if (existing.stream_status === "completed" && incoming.stream_status !== "completed") {
    return messages
  }
  const next = [...messages]
  next[index] = { ...existing, ...incoming, id: existing.id }
  return next.sort(compareMessages)
}

function compareMessages(left: Message, right: Message): number {
  const timeDelta = Date.parse(left.timestamp) - Date.parse(right.timestamp)
  return Number.isFinite(timeDelta) && timeDelta !== 0
    ? timeDelta
    : left.id.localeCompare(right.id)
}

function upsertTimeline(
  timeline: ExecutionTimelineEntry[],
  incoming: ExecutionTimelineEntry,
): ExecutionTimelineEntry[] {
  const index = timeline.findIndex((entry) => (
    entry.id === incoming.id || timelineEntriesMatch(entry, incoming)
  ))
  if (index < 0) {
    return [...timeline, incoming].sort(compareTimeline)
  }
  if (timeline[index].status === "completed" && incoming.status !== "completed") {
    return timeline
  }
  const next = [...timeline]
  next[index] = { ...timeline[index], ...incoming }
  return next.sort(compareTimeline)
}

function compareTimeline(left: ExecutionTimelineEntry, right: ExecutionTimelineEntry): number {
  return left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id)
}

function progressTimeline(
  item: { time: string; content: string },
  options: {
    id: string
    runId: string | null
    source: string
    taskTitle?: string
    workerName?: string
  },
): ExecutionTimelineEntry {
  const { id, runId, source, taskTitle, workerName } = options
  const sourceLabel = source === "root"
    ? "Root"
    : source === "worker"
      ? workerName || "Worker"
      : "任务调度"
  return {
    id,
    run_id: runId ?? undefined,
    event_type: `progress.${source}`,
    label: item.content,
    detail: taskTitle ? `${taskTitle} · ${sourceLabel}` : sourceLabel,
    status: "info",
    timestamp: item.time,
  }
}

function taskResultTimeline(
  payload: Record<string, unknown>,
  runId: string | null,
  fallbackTime: string,
): ExecutionTimelineEntry | null {
  const result = objectValue(payload.result)
  const content = text(result.content)
  if (!content) return null
  const taskTitle = text(payload.task_title) || "Worker 任务"
  const workerName = text(payload.worker_name) || "Worker"
  const status = text(payload.status)
  return {
    id: `task-result:${runId || "session"}:${text(payload.execution_id) || taskTitle}`,
    run_id: runId ?? undefined,
    event_type: "task.result",
    label: taskTitle,
    detail: content,
    status: status === "failed" || (!status && content.includes("失败"))
      ? "failed"
      : status === "cancelled" || (!status && content.includes("取消"))
        ? "cancelled"
        : "completed",
    timestamp: progressTime(result.time, fallbackTime),
    metadata: { worker_name: workerName, task_title: taskTitle },
  }
}

function projectEnvelope(
  view: View,
  envelope: ApplicationEventEnvelope,
): View {
  const type = envelope.event_type
  const payload = envelope.payload || {}
  const runId = envelope.run_id || null
  const occurredAt = envelope.occurred_at || new Date().toISOString()
  if (type === "session.snapshot") {
    return applySessionSnapshot(
      view,
      payload as unknown as SessionSnapshotPayload,
    )
  }
  if (type === "turn.started") {
    return { ...view, agentStatus: "running", error: null, errorDetail: null }
  }
  if (type === "progress.created") {
    const content = text(payload.content)
    if (!content) return view
    const source = text(payload.source) || "orchestrator"
    const item = {
      time: progressTime(payload.created_at, occurredAt),
      content,
    }
    return {
      ...view,
      executionTimeline: upsertTimeline(
        view.executionTimeline || [],
        progressTimeline(
          item,
          {
            id: `progress:${text(payload.progress_id) || `${runId}:${payload.sequence}`}`,
            runId,
            source,
            taskTitle: text(payload.task_title) || undefined,
            workerName: text(payload.worker_name) || undefined,
          },
        ),
      ),
    }
  }
  if (type === "task.result") {
    const entry = taskResultTimeline(payload, runId, occurredAt)
    return entry
      ? {
          ...view,
          executionTimeline: upsertTimeline(view.executionTimeline || [], entry),
        }
      : view
  }
  if (type === "assistant.started") {
    const streamId = envelope.stream_id || (runId ? `answer-${runId}` : undefined)
    return {
      ...view,
      messages: upsertMessage(view.messages || [], {
        id: envelope.message_id || `assistant:${runId || "stream"}`,
        role: "assistant",
        content: "",
        timestamp: text(payload.started_at) || occurredAt,
        run_id: runId ?? undefined,
        stream_id: streamId,
        stream_status: "streaming",
        last_chunk_id: 0,
      }),
    }
  }
  if (type === "assistant.delta") {
    const delta = text(payload.delta)
    const chunkId = Number(envelope.chunk_id || payload.chunk_id || 0)
    const canonicalId = envelope.message_id || `assistant:${runId || "stream"}`
    const existing = (view.messages || []).find((message) => (
      message.id === canonicalId
      || (runId && message.run_id === runId && message.role === "assistant")
    ))
    if (!delta || (existing?.last_chunk_id && chunkId <= existing.last_chunk_id)) {
      return view
    }
    return {
      ...view,
      messages: upsertMessage(view.messages || [], {
        id: existing?.id || canonicalId,
        role: "assistant",
        content: `${existing?.content || ""}${delta}`,
        timestamp: existing?.timestamp || occurredAt,
        run_id: runId ?? existing?.run_id,
        stream_id: envelope.stream_id || existing?.stream_id,
        stream_status: "streaming",
        last_chunk_id: chunkId || existing?.last_chunk_id,
      }),
    }
  }
  if (type === "assistant.completed") {
    const canonicalId = envelope.message_id || `assistant:${runId || "stream"}`
    const existing = (view.messages || []).find((message) => (
      message.id === canonicalId
      || (runId && message.run_id === runId && message.role === "assistant")
    ))
    return {
      ...view,
      agentStatus: "completed",
      messages: upsertMessage(view.messages || [], {
        id: existing?.id || canonicalId,
        role: "assistant",
        content: text(payload.content),
        timestamp: text(payload.started_at) || existing?.timestamp || occurredAt,
        run_id: runId ?? existing?.run_id,
        stream_id: envelope.stream_id || existing?.stream_id,
        stream_status: "completed",
        last_chunk_id: Number(envelope.chunk_id || existing?.last_chunk_id || 0),
      }),
    }
  }
  if (type === "turn.failed") {
    const error = objectValue(payload.error)
    return {
      ...view,
      agentStatus: "error",
      error: text(error.message) || "任务未能完成",
      errorDetail: {
        code: text(error.code) || "run_failed",
        message: text(error.message) || "任务未能完成",
        retryable: Boolean(error.retryable),
      },
      messages: (view.messages || []).filter((message) => !(
        message.run_id === runId && message.stream_status === "streaming"
      )),
    }
  }
  if (type === "turn.cancelled") {
    return {
      ...view,
      agentStatus: "idle",
      error: null,
      errorDetail: null,
      messages: (view.messages || []).filter((message) => !(
        message.run_id === runId && message.stream_status === "streaming"
      )),
    }
  }
  return view
}

function applyEnvelope(
  sessionId: string,
  envelope: ApplicationEventEnvelope,
  cacheOnly = false,
): void {
  const type = envelope.event_type
  const runId = envelope.run_id || null
  useChatStore.setState((state) => {
    const active = state.activeSessionId === sessionId && !cacheOnly
    const current = active
      ? {
          messages: state.messages,
          executionTimeline: state.executionTimeline,
          agentStatus: state.agentStatus,
          error: state.error,
          errorDetail: state.errorDetail,
        }
      : state.sessionViewCache[sessionId] || EMPTY_VIEW
    const next = projectEnvelope(current, envelope)
    if (active) {
      return {
        messages: next.messages || state.messages,
        executionTimeline: next.executionTimeline || state.executionTimeline,
        agentStatus: next.agentStatus || state.agentStatus,
        error: next.error === undefined ? state.error : next.error,
        errorDetail: next.errorDetail === undefined ? state.errorDetail : next.errorDetail,
      }
    }
    return {
      sessionViewCache: {
        ...state.sessionViewCache,
        [sessionId]: { ...state.sessionViewCache[sessionId], ...next },
      },
    }
  })

  const snapshotPayload = type === "session.snapshot"
    ? (envelope.payload || {}) as unknown as SessionSnapshotPayload
    : null
  if (snapshotPayload) {
    for (const turn of snapshotPayload.turns) {
      useChatStore.getState().setSessionRunStatus(
        sessionId,
        turn.run_id,
        turn.run_status,
      )
    }
  }
  const runStatus = type === "turn.failed"
    ? "failed"
    : type === "turn.cancelled"
      ? "cancelled"
      : type === "assistant.completed"
        ? "completed"
        : type === "turn.started"
          ? "running"
          : snapshotPayload
            ? snapshotPayload.run_status
            : null
  if (runId && runStatus) {
    useChatStore.getState().setSessionRunStatus(sessionId, runId, runStatus)
    const terminal = ["completed", "failed", "cancelled"].includes(runStatus)
    useChatStore.getState().updateSession(sessionId, {
      active_run_id: terminal ? null : runId,
    })
  }
}

function subscribe(
  sessionId: string,
  apiBase: string,
  syncToken: string,
  options: {
    cacheOnly: boolean
    disposed: () => boolean
    onConnected: () => void
    onError: () => void
  },
): EventSource {
  const {
    cacheOnly,
    disposed,
    onConnected,
    onError,
  } = options
  const source = new EventSource(
    sessionEventStreamUrl(apiBase, sessionId, syncToken),
    { withCredentials: true },
  )
  source.onopen = () => {
    if (!disposed()) onConnected()
  }
  source.onerror = () => {
    if (!disposed()) onError()
  }
  for (const name of SESSION_EVENT_NAMES) {
    source.addEventListener(name, (event) => {
      if (disposed()) return
      try {
        const envelope = JSON.parse((event as MessageEvent<string>).data) as ApplicationEventEnvelope
        applyEnvelope(sessionId, envelope, cacheOnly)
      } catch {
        // 单条损坏事件不能中断后续聊天流。
      }
    })
  }
  return source
}

/** 订阅当前会话的用户可见聊天流，并维护内存中的流式回答。 */
export function useSessionEventStream(sessionId: string | null, apiBase: string) {
  const sourceRef = useRef<EventSource | null>(null)
  const ready = useChatStore((state) => (
    Boolean(sessionId && state.sessionHistoryLoaded[sessionId])
  ))
  const syncToken = useChatStore((state) => (
    sessionId ? state.sessionSyncTokens[sessionId] : undefined
  ))

  useEffect(() => {
    let disposed = false
    sourceRef.current?.close()
    sourceRef.current = null
    if (!sessionId || !ready || !syncToken) {
      useChatStore.getState().setConnectionStatus("disconnected")
      return
    }
    useChatStore.getState().setConnectionStatus("connecting")
    sourceRef.current = subscribe(
      sessionId,
      apiBase,
      syncToken,
      {
        cacheOnly: false,
        disposed: () => disposed,
        onConnected: () => useChatStore.getState().setConnectionStatus("connected"),
        onError: () => useChatStore.getState().setConnectionStatus("error"),
      },
    )
    return () => {
      disposed = true
      sourceRef.current?.close()
      sourceRef.current = null
    }
  }, [apiBase, ready, sessionId, syncToken])

  return sourceRef
}

/** 只对已经完成首次历史渲染的后台会话继续接收并缓存聊天事件。 */
export function useBackgroundSessionEventStream(sessionId: string | null, apiBase: string) {
  const ready = useChatStore((state) => (
    Boolean(sessionId && state.sessionHistoryLoaded[sessionId])
  ))
  const syncToken = useChatStore((state) => (
    sessionId ? state.sessionSyncTokens[sessionId] : undefined
  ))

  useEffect(() => {
    if (!sessionId || !ready || !syncToken) return
    let disposed = false
    const source = subscribe(
      sessionId,
      apiBase,
      syncToken,
      {
        cacheOnly: true,
        disposed: () => disposed,
        onConnected: () => undefined,
        onError: () => undefined,
      },
    )
    return () => {
      disposed = true
      source.close()
    }
  }, [apiBase, ready, sessionId, syncToken])
}
