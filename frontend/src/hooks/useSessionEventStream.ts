import { useEffect, useRef } from "react"
import { useChatStore, type SessionViewState } from "../store/chatStore"
import { closeTimelineEntry, workerResultText } from "../utils/executionTimeline.ts"
import {
  backgroundSessionCanRelease,
  cacheBackgroundLlmStep,
  cacheSessionTimelineProjection,
  hasCompletedAssistant,
  projectLlmValidationFailureTimeline,
  sessionEventResumeCursor,
  sessionEventStreamUrl,
} from "../utils/sessionHistory.ts"
import { SESSION_EVENT_NAMES } from "../utils/sessionEventCatalog.ts"
import { apiClient } from "../api/client"
import type { ApplicationEventEnvelope, ExecutionError } from "../types/events"
import type { ExecutionTimelineEntry, Message, ToolCall, OrchestrationPlan, OrchestrationTask, RunTimelineEvent } from "../types"
import { approvalActionDescription, toolActionLabel } from "../utils/executionCopy"
import { summarizeResult } from "../utils/orchestrationProjection"
import { shouldHandleStreamCallback } from "../utils/workerSnapshotProjection"
import { canApplyRunStatusEvent, preserveTerminalRunStatuses, runStatusFromEvent, shouldApplyRunStatusSnapshot, summarizeSessionRuns } from "../utils/runLifecycle"

interface StreamProjection {
  nextChunkId: number
  finalChunkId: number | null
  content: string
  status: "created" | "streaming" | "completed" | "paused" | "failed" | "cancelled"
  pending: Map<number, string>
  snapshotVersion: number
  startedSeq?: number
  startedAt?: string
  repairAttempts: number
}

const processedSessionCursors = new Map<string, number>()
const backgroundReceivedCursors = new Map<string, number>()
type BackgroundStreamState = { content: string; nextChunkId: number; pending: Map<number, string>; status: Message["stream_status"] }
const backgroundSessionStreams = new Map<string, Map<string, BackgroundStreamState>>()

export function clearSessionEventCache(sessionId: string): void {
  processedSessionCursors.delete(sessionId)
  backgroundReceivedCursors.delete(sessionId)
  backgroundSessionStreams.delete(sessionId)
}

function cacheBackgroundEvent(sessionId: string, envelope: ApplicationEventEnvelope): void {
  const runId = envelope.run_id || ""
  const type = envelope.event_type || ""
  const payload = envelope.payload || {}
  const timestamp = envelope.occurred_at || new Date().toISOString()
  const store = useChatStore.getState()
  const previous = store.sessionViewCache[sessionId] || {}
  const runStatus = store.sessionRunStatuses[sessionId]?.[runId]
  const next: Partial<SessionViewState> = { ...previous }

  const timeline = projectTimelineEvent({
    type,
    data: payload,
    envelope,
    runId,
    timestamp,
  })
  Object.assign(
    next,
    cacheSessionTimelineProjection(previous, envelope, timeline),
  )
  const cachedSteps = cacheBackgroundLlmStep(previous.steps, sessionId, envelope)
  if (cachedSteps) next.steps = cachedSteps
  if (timeline) {
    const markers = previous.executionTimelineTerminalMarkers || {}
    let projectedEntries = next.executionTimeline || previous.executionTimeline || []
    const terminalTaskEvents = ["task.completed", "task.failed", "task.cancelled", "task.cancelling", "task.retrying"]
    if (terminalTaskEvents.includes(type)) {
      const started = projectTimelineEvent({ type: "task.started", data: payload, envelope, runId, timestamp })
      if (started) {
        const closed = closeTimelineEntry(projectedEntries, markers, { ...started, status: timeline.status })
        projectedEntries = closed.entries
        next.executionTimelineTerminalMarkers = closed.terminalMarkers
      }
    }
    next.executionTimeline = projectedEntries
  }

  if (type === "thinking.started" || type === "thinking.summary") {
    next.thinking = { active: true, content: String(payload.content || ""), messageId: envelope.stream_id || null }
  } else if (type === "thinking.completed") {
    next.thinking = null
  }
  if (type === "approval.required") {
    const approvalId = typeof payload.approval_id === "string" ? payload.approval_id : ""
    if (approvalId) {
      const approvals = [...(previous.pendingApprovals || [])]
      const request = {
        approval_id: approvalId,
        tool_name: typeof payload.tool_name === "string" ? payload.tool_name : "tool",
        arguments: payload.arguments && typeof payload.arguments === "object" ? payload.arguments as Record<string, unknown> : {},
        risk_level: (typeof payload.risk_level === "string" ? payload.risk_level : "low") as "low" | "medium" | "high" | "critical",
        session_id: sessionId,
        run_id: runId || undefined,
        plan_id: typeof payload.plan_id === "string" ? payload.plan_id : undefined,
        task_id: typeof payload.task_id === "string" ? payload.task_id : undefined,
        task_title: typeof payload.task_title === "string" ? payload.task_title : undefined,
        worker_run_id: typeof payload.worker_run_id === "string" ? payload.worker_run_id : undefined,
      }
      next.pendingApprovals = approvals.some((item) => item.approval_id === approvalId)
        ? approvals.map((item) => item.approval_id === approvalId ? { ...item, ...request } : item)
        : [...approvals, request]
    }
  } else if (type === "approval.resolved" && typeof payload.approval_id === "string") {
    next.pendingApprovals = (previous.pendingApprovals || []).filter((item) => item.approval_id !== payload.approval_id)
  }

  if (["message.started", "message.delta", "message.completed", "stream.snapshot"].includes(type) && runId) {
    const messages = [...(previous.messages || [])]
    const messageId = envelope.message_id || String(payload.message_id || `assistant:${runId}`)
    const existingIndex = messages.findIndex((message) => message.id === messageId || message.run_id === runId && message.role === "assistant")
    const existing = existingIndex >= 0 ? messages[existingIndex] : undefined
    if (existing?.stream_status === "completed") return
    if (["completed", "failed", "cancelled"].includes(String(runStatus))
      && ["message.started", "message.delta", "stream.snapshot"].includes(type)) return
    const streamId = envelope.stream_id || `answer-${runId}`
    let sessionStreams = backgroundSessionStreams.get(sessionId)
    if (!sessionStreams) {
      sessionStreams = new Map()
      backgroundSessionStreams.set(sessionId, sessionStreams)
    }
    const stream = sessionStreams.get(streamId) || {
      content: existing?.content || "",
      nextChunkId: (existing?.last_chunk_id || 0) + 1,
      pending: new Map<number, string>(),
      status: existing?.stream_status || "streaming",
    }
    const finalContent = typeof payload.content === "string" ? payload.content : undefined
    if (type === "message.delta") {
      const delta = typeof payload.delta === "string" ? payload.delta : typeof payload.token === "string" ? payload.token : ""
      const chunkId = envelope.chunk_id ?? Number(payload.chunk_id || 0)
      if (delta && chunkId > 0) {
        stream.pending.set(chunkId, delta)
        while (stream.pending.has(stream.nextChunkId)) {
          stream.content += stream.pending.get(stream.nextChunkId) || ""
          stream.pending.delete(stream.nextChunkId)
          stream.nextChunkId += 1
        }
      } else if (delta) stream.content += delta
      stream.status = "streaming"
    } else if (type === "stream.snapshot") {
      stream.content = String(payload.content || "")
      stream.nextChunkId = Number(payload.last_chunk_id || 0) + 1
      stream.pending.clear()
      stream.status = payload.status === "completed" ? "completed" : "streaming"
    } else if (type === "message.completed") {
      stream.content = finalContent ?? stream.content
      stream.status = "completed"
    } else {
      stream.status = "streaming"
    }
    sessionStreams.set(streamId, stream)
    const content = stream.content
    const message: Message = {
      id: messageId,
      role: "assistant",
      content,
      timestamp: String(payload.started_at || timestamp),
      session_id: sessionId,
      run_id: runId,
      stream_id: envelope.stream_id || undefined,
      stream_status: stream.status,
      last_chunk_id: stream.nextChunkId - 1,
    }
    if (existingIndex >= 0) messages[existingIndex] = { ...existing, ...message }
    else messages.push(message)
    next.messages = messages
  }

  useChatStore.setState((state) => ({
    sessionViewCache: {
      ...state.sessionViewCache,
      [sessionId]: { ...state.sessionViewCache[sessionId], ...next },
    },
  }))
  const status = runStatusFromEvent(type)
  if (runId && status) store.setSessionRunStatus(sessionId, runId, status)
}

function applyPendingChunks(stream: StreamProjection) {
  while (stream.pending.has(stream.nextChunkId)) {
    stream.content += stream.pending.get(stream.nextChunkId) || ""
    stream.pending.delete(stream.nextChunkId)
    stream.nextChunkId += 1
  }
}

/**
 * 订阅会话级有序 SSE。session_seq 负责事件去重，stream_id/chunk_id 负责内容组装。
 * Snapshot 只修复流状态，不覆盖已经按 Chunk 组装出的更新版本。
 */
export function useSessionEventStream(sessionId: string | null, apiBase: string) {
  const sourceRef = useRef<EventSource | null>(null)
  const lastSeqRef = useRef(0)
  const streamsRef = useRef(new Map<string, StreamProjection>())
  const {
    setConnectionStatus, setAgentStatus, addMessage, updateMessage, removeMessage, setAttachments,
    addStep, updateStep, addToolCall, updateToolCall, addApproval, mergeApprovals, resolveApproval,
    updateSession,
    setSessionRunStatus, replaceSessionRunStatuses,
    setSessionEventCursor, setSteps,
    clearSteps, clearToolCalls, clearApprovals, clearThinking, setThinking, setError, setErrorDetail, clearErrorDetail,
    upsertOrchestrationTask, upsertOrchestrationPlan,
    beginOrchestrationSnapshotRequest, setOrchestrationSnapshot,
    upsertExecutionTimelineEntry, closeExecutionTimelineEntry,
    appendRunTimelineEvent,
  } = useChatStore()

  useEffect(() => {
    let disposed = false
    sourceRef.current?.close()
    sourceRef.current = null
    lastSeqRef.current = sessionId
      ? sessionEventResumeCursor(
        undefined,
        processedSessionCursors.get(sessionId) || 0,
        backgroundReceivedCursors.get(sessionId) || 0,
      )
      : 0
    streamsRef.current.clear()
    let historyEventCursor: number | undefined
    if (sessionId) {
      historyEventCursor = useChatStore.getState().sessionEventCursors[sessionId]
      for (const message of useChatStore.getState().messages) {
        if (message.role !== "assistant" || !message.run_id || !message.stream_id || message.stream_status !== "streaming") continue
        streamsRef.current.set(message.stream_id, {
          content: message.content,
          status: "streaming",
          nextChunkId: (message.last_chunk_id || 0) + 1,
          finalChunkId: null,
          pending: new Map<number, string>(),
          snapshotVersion: 0,
          repairAttempts: 0,
        })
      }
    } else {
      setConnectionStatus("disconnected")
      return
    }

    let currentSource: EventSource | null = null
    let connectionGeneration = 0
    let recoveryTarget: string | null = null
    let recoveryUntil = 0
    let repairStream: (streamId: string, stream: StreamProjection, runId: string) => void = () => {}
    setConnectionStatus("connecting")
    const syncPendingApprovals = () => {
      void apiClient.listPendingApprovals(sessionId)
        .then((approvals) => {
          if (disposed || useChatStore.getState().activeSessionId !== sessionId) return
          const pending = approvals.map((approval) => ({
            ...approval,
            session_id: approval.session_id || sessionId,
          }))
          mergeApprovals(pending)
          if (pending.length > 0 && useChatStore.getState().agentStatus !== "cancelling") setAgentStatus("waiting_approval")
        })
        .catch(() => {
          // API 暂时不可用时保留已有事件状态，等待下一次重连同步。
        })
    }
    const orchestrationRefreshGeneration = new Map<string, number>()
    const orchestrationRefreshTimers = new Map<string, ReturnType<typeof setTimeout>>()
    const refreshOrchestrationSnapshot = (runId: string) => {
      if (!runId) return
      const previous = orchestrationRefreshTimers.get(runId)
      if (previous) clearTimeout(previous)
      orchestrationRefreshTimers.set(runId, setTimeout(() => {
        if (disposed) return
        const generation = beginOrchestrationSnapshotRequest(runId)
        orchestrationRefreshGeneration.set(runId, generation)
        void apiClient.getRunOrchestration(runId)
          .then((snapshot) => {
            if (disposed || useChatStore.getState().activeSessionId !== sessionId || orchestrationRefreshGeneration.get(runId) !== generation) return
            setOrchestrationSnapshot(snapshot, generation)
          })
          .catch(() => {
            // SSE 只负责触发刷新；快照接口短暂不可用时保留最后一个持久快照。
          })
      }, 150))
    }
    let lifecycleSyncGeneration = 0
    let lifecycleRevision = 0
    const syncRunLifecycle = async () => {
      const generation = ++lifecycleSyncGeneration
      const revision = lifecycleRevision
      const storeRevision = useChatStore.getState().sessionRunStatusRevisions[sessionId] || 0
      try {
        const runs = await apiClient.listSessionRuns(sessionId)
        const currentStoreRevision = useChatStore.getState().sessionRunStatusRevisions[sessionId] || 0
        if (!shouldApplyRunStatusSnapshot({
          disposed,
          snapshotSessionId: sessionId,
          activeSessionId: useChatStore.getState().activeSessionId,
          snapshotRevision: revision,
          currentRevision: lifecycleRevision,
          snapshotStoreRevision: storeRevision,
          currentStoreRevision,
          requestGeneration: generation,
          currentGeneration: lifecycleSyncGeneration,
        })) return
        const statuses = preserveTerminalRunStatuses(
          useChatStore.getState().sessionRunStatuses[sessionId] || {},
          Object.fromEntries(runs.map((run) => [run.run_id, run.status])),
        )
        if (!replaceSessionRunStatuses(sessionId, statuses, storeRevision)) return
        const { activeRunId, agentStatus } = summarizeSessionRuns(statuses)
        const active = activeRunId ? runs.find((run) => run.run_id === activeRunId) : undefined
        if (active) refreshOrchestrationSnapshot(active.run_id)
        updateSession(sessionId, { active_run_id: activeRunId })
        setAgentStatus(agentStatus === "idle" && runs.at(-1)?.status === "failed" ? "error" : agentStatus)
      } catch {
        // Keep event-derived state while the status query is temporarily unavailable.
      }
    }
    const onOpen = () => {
      if (!shouldHandleStreamCallback(disposed, 0, connectionGeneration)) return
      setConnectionStatus("connected")
      syncPendingApprovals()
      void syncRunLifecycle()
    }
    const onError = () => {
      if (!shouldHandleStreamCallback(disposed, 0, connectionGeneration)) return
      setConnectionStatus("error")
    }
    syncPendingApprovals()
    void syncRunLifecycle()

    const upsertAssistant = (
      runId: string,
      content: string,
      occurredAt: string,
      streamId?: string,
      messageId?: string,
      streamStatus?: Message["stream_status"],
      lastChunkId?: number,
    ) => {
      if (!runId) return
      const state = useChatStore.getState()
      const canonicalId = messageId || `assistant:${runId}`
      const existingById = state.messages.find((m) => m.id === canonicalId)
      const existingByRun = state.messages.find((m) => m.run_id === runId && m.role === "assistant")
      const existing = existingById || existingByRun
      if (existingById && existingByRun && existingById.id !== existingByRun.id) {
        removeMessage(existingByRun.id)
      }
      if (existing) {
        // 完成态是单调终态，旧 started/delta/snapshot 不能覆盖已落库正文。
        if (existing.stream_status === "completed") return
        if (!existing.stream_status && streamStatus !== "completed") return
        // Replayed events can repair a timestamp previously set to receipt time.
        // Later chunks must not move the message's original event time forward.
        const timestamp = Date.parse(occurredAt) < Date.parse(existing.timestamp)
          ? occurredAt : existing.timestamp
        if (existing.id !== canonicalId || existing.content !== content || existing.timestamp !== timestamp || existing.stream_status !== streamStatus || existing.last_chunk_id !== lastChunkId) {
          updateMessage(existing.id, { id: canonicalId, content, timestamp, stream_status: streamStatus, stream_id: streamId, last_chunk_id: lastChunkId })
        }
        return
      }
      addMessage({
        id: canonicalId, role: "assistant", content,
        timestamp: occurredAt, session_id: sessionId, run_id: runId,
        stream_status: streamStatus,
        stream_id: streamId,
        last_chunk_id: lastChunkId,
        ...(streamId ? { type: `stream:${streamId}` } : {}),
      } as Message)
    }

    const closeRunStreams = (runId: string, status: "paused" | "failed" | "cancelled") => {
      for (const [streamId, stream] of streamsRef.current) {
        const message = useChatStore.getState().messages.find((item) => item.stream_id === streamId || item.run_id === runId && item.role === "assistant")
        if (!message || message.run_id !== runId || message.stream_status === "completed") continue
        stream.status = status
        updateMessage(message.id, { stream_status: status, last_chunk_id: stream.nextChunkId - 1 })
      }
    }

    const parse = (event: MessageEvent, generation = connectionGeneration) => {
      try {
        if (!shouldHandleStreamCallback(disposed, generation, connectionGeneration)) return
        const envelope = JSON.parse(event.data) as ApplicationEventEnvelope
        const type = envelope.event_type || event.type
        const data: Record<string, unknown> = envelope.payload ?? (envelope as Record<string, unknown>)
        const errorDetail = (data.error_detail || envelope.error_detail) as ExecutionError | undefined
        const sequence = envelope.session_seq
        if (typeof sequence === "number") {
          if (sequence <= lastSeqRef.current) return
          lastSeqRef.current = sequence
          processedSessionCursors.set(sessionId, sequence)
          backgroundReceivedCursors.set(sessionId, Math.max(backgroundReceivedCursors.get(sessionId) || 0, sequence))
        }
        const occurredAt = envelope.occurred_at || new Date().toISOString()
        const runId = envelope.run_id || String(data.run_id || "")
        if (type.startsWith("run.")) lifecycleRevision += 1
        const incomingRunStatus = runStatusFromEvent(type)
        if (runId && incomingRunStatus && !canApplyRunStatusEvent(useChatStore.getState().sessionRunStatuses[sessionId]?.[runId], incomingRunStatus)) return
        const currentRunStatus = runId ? useChatStore.getState().sessionRunStatuses[sessionId]?.[runId] : undefined
        if (runId && ["completed", "failed", "cancelled"].includes(String(currentRunStatus))
          && ["message.started", "message.delta", "stream.snapshot"].includes(type)) return
        if (runId && (type.startsWith("plan.") || type.startsWith("task.") || type.startsWith("approval.") || type.startsWith("run."))) {
          refreshOrchestrationSnapshot(runId)
        }
        const streamId = envelope.stream_id || String(data.stream_id || (runId ? `answer-${runId}` : ""))
        const outputEvents = [
          "message.started",
          "message.delta",
          "message.completed",
          "stream.snapshot",
          "thinking.started",
          "thinking.summary",
          "thinking.completed",
        ]
        const outputCutoff = runId
          ? useChatStore.getState().suppressedRunOutput[runId]
          : undefined
        if (outputEvents.includes(type) && outputCutoff !== undefined && (sequence === undefined || sequence > outputCutoff)) return
        if (runId && typeof sequence === "number") {
          appendRunTimelineEvent({
            session_seq: sequence,
            event_type: type,
            occurred_at: occurredAt,
            run_id: runId,
            payload: data as RunTimelineEvent["payload"],
          })
        }
        if (recoveryTarget && typeof sequence === "number" && sequence <= recoveryUntil) {
          const isTargetStreamEvent = type.startsWith("message.") && streamId === recoveryTarget
          if (!isTargetStreamEvent) {
            if (sequence >= recoveryUntil) recoveryTarget = null
            return
          }
          if (sequence >= recoveryUntil) recoveryTarget = null
        }
        const timelineEntry = projectTimelineEvent({
          type,
          data,
          envelope,
          runId,
          timestamp: occurredAt,
        })
        if (timelineEntry) upsertExecutionTimelineEntry(timelineEntry)
        if (["task.completed", "task.failed", "task.cancelled", "task.cancelling", "task.retrying"].includes(type)) {
          const startedEntry = projectTimelineEvent({
            type: "task.started",
            data,
            envelope,
            runId,
            timestamp: occurredAt,
          })
          if (startedEntry && timelineEntry) {
            closeExecutionTimelineEntry({
              ...startedEntry,
              status: timelineEntry.status,
            })
          }
          if (type === "task.cancelled" && timelineEntry) {
            const cancellingEntry = projectTimelineEvent({
              type: "task.cancelling",
              data,
              envelope,
              runId,
              timestamp: occurredAt,
            })
            if (cancellingEntry) {
              closeExecutionTimelineEntry({
                ...cancellingEntry,
                status: timelineEntry.status,
              })
            }
          }
        }
        if (type === "message.persisted") {
          const persistedMessageId = String(data.message_id || envelope.message_id || "")
          if (persistedMessageId && data.role === "user") {
            const persistedTimestamp = String(data.timestamp || occurredAt)
            const requestId = text(data.request_id)
            if (requestId) removeMessage(requestId)
            const existing = useChatStore.getState().messages.find((item) => item.id === persistedMessageId)
            if (existing && (existing.timestamp !== persistedTimestamp || existing.run_id !== runId || existing.session_seq !== sequence)) {
              updateMessage(persistedMessageId, { timestamp: persistedTimestamp, run_id: runId || undefined, session_seq: sequence })
            } else if (!existing && typeof data.content === "string") {
              addMessage({
                id: persistedMessageId,
                role: "user",
                content: data.content,
                timestamp: persistedTimestamp,
                session_id: sessionId,
                run_id: runId || undefined,
                session_seq: sequence,
              })
            }
          }
          if (Array.isArray(data.attachment_ids) && data.attachment_ids.length > 0) {
            void apiClient.listAttachments(sessionId).then((items) => {
              if (!disposed && useChatStore.getState().activeSessionId === sessionId) setAttachments(items)
            }).catch(() => {})
          }
        }

        if (type === "stream.snapshot") {
          const version = Number(data.version || 0)
          if (data.stream_type === "thinking") {
            // Thinking is a durable event stream, never a recoverable snapshot.
            return
          }
          const stream = streamsRef.current.get(streamId) || {
            nextChunkId: 1, finalChunkId: null, content: "", status: "created",
            pending: new Map<number, string>(), snapshotVersion: 0, repairAttempts: 0,
          }
          if (version >= stream.snapshotVersion) {
            stream.content = String(data.content || "")
            stream.snapshotVersion = version
            stream.status = data.status === "completed" ? "completed" : "streaming"
            stream.pending.clear()
            stream.nextChunkId = Number(data.last_chunk_id || 0) + 1
            streamsRef.current.set(streamId, stream)
            upsertAssistant(runId, stream.content, occurredAt, streamId, envelope.message_id || undefined)
          }
          return
        }

        if (type === "message.started") {
          const stream = streamsRef.current.get(streamId) || {
            nextChunkId: 1, finalChunkId: null, content: "", status: "created",
            pending: new Map<number, string>(), snapshotVersion: 0, repairAttempts: 0,
          }
          stream.startedSeq = sequence
          stream.startedAt = String(data.started_at || occurredAt)
          streamsRef.current.set(streamId, stream)
          upsertAssistant(runId, stream.content, occurredAt, streamId, envelope.message_id || undefined, "streaming", 0)
        } else if (type === "message.delta") {
          const stream = streamsRef.current.get(streamId) || {
            nextChunkId: 1, finalChunkId: null, content: "", status: "created",
            pending: new Map<number, string>(), snapshotVersion: 0, repairAttempts: 0,
          }
          const delta = String(data.delta || data.token || "")
          const chunkId = envelope.chunk_id ?? Number(data.chunk_id || 0)
          if (!delta) return
          if (chunkId > 0) {
            if (chunkId < stream.nextChunkId) return
            if (chunkId > stream.nextChunkId) {
              if (stream.repairAttempts < 1) {
                repairStream(streamId, stream, runId)
              } else {
                setError("回答片段恢复失败，保留目前已恢复的正文。")
                const current = useChatStore.getState().messages.find((item) => item.run_id === runId && item.role === "assistant")
                if (current) updateMessage(current.id, { stream_status: "unconfirmed" })
              }
              return
            }
            stream.pending.set(chunkId, delta)
            applyPendingChunks(stream)
          } else {
            stream.content += delta
          }
          stream.status = "streaming"
          if (envelope.is_complete) {
            stream.finalChunkId = chunkId || stream.nextChunkId - 1
            stream.status = "completed"
          }
          streamsRef.current.set(streamId, stream)
          upsertAssistant(runId, stream.content, occurredAt, streamId, envelope.message_id || undefined, stream.status === "completed" ? "completed" : "streaming", stream.nextChunkId - 1)
        } else if (type === "message.completed") {
          const stream = streamsRef.current.get(streamId)
          if (stream) stream.status = "completed"
          const finalContent = typeof data.content === "string" ? data.content : stream?.content || ""
          const finalChunkId = Number(
            data.last_chunk_id ?? envelope.chunk_id ?? (stream ? stream.nextChunkId - 1 : 0)
          )
          const messageId = envelope.message_id || String(data.message_id || `assistant:${runId}`)
          upsertAssistant(runId, finalContent, String(data.started_at || occurredAt), streamId, messageId, "completed", finalChunkId)
        } else if (type === "thinking.started" || type === "thinking.summary") {
          setThinking({ active: true, content: String(data.content || ""), messageId: streamId || null })
          if (useChatStore.getState().agentStatus !== "cancelling") setAgentStatus("thinking")
        } else if (type === "thinking.completed") {
          clearThinking()
        }

        if (type === "approval.required") {
          addApproval({ approval_id: String(data.approval_id || ""), tool_name: String(data.tool_name || "tool"), arguments: (data.arguments as Record<string, unknown>) || {}, risk_level: (String(data.risk_level || "low") as "low" | "medium" | "high"), approval_batch_id: data.approval_batch_id ? String(data.approval_batch_id) : null, session_id: sessionId, run_id: runId || undefined, plan_id: text(data.plan_id) || undefined, task_id: text(data.task_id) || undefined, task_title: text(data.task_title) || undefined, worker_run_id: text(data.worker_run_id) || undefined, expires_at: text(data.expires_at) || undefined })
          if (useChatStore.getState().agentStatus !== "cancelling") setAgentStatus("waiting_approval")
        } else if (type === "approval.resolved") {
          const decisions = (data.decisions as Record<string, string> | undefined) || {}
          const approvalId = String(data.approval_id || "")
          if (approvalId) decisions[approvalId] = String(data.decision || "denied")
          Object.entries(decisions).forEach(([id, decision]) => resolveApproval(id, decision))
          if (useChatStore.getState().pendingApprovals.length <= Object.keys(decisions).length && useChatStore.getState().agentStatus !== "cancelling") setAgentStatus("running")
        }
        if (type.startsWith("plan.")) {
          const planStatus: OrchestrationPlan["status"] | null =
            type === "plan.created" || type === "plan.revised" ? "running"
            : type === "plan.cancelling" ? "cancelling"
            : type === "plan.completed" ? "completed"
            : type === "plan.failed" ? "failed"
            : type === "plan.cancelled" ? "cancelled"
            : null
          const plan: OrchestrationPlan = {
            plan_id: text(data.plan_id) || `plan:${runId}`,
            run_id: runId || undefined,
            goal: text(data.goal) || text(data.user_goal) || undefined,
            status: planStatus || "running",
            max_parallelism: number(data.max_parallelism),
            task_ids: stringArray(data.task_ids),
            session_seq: sequence,
            transition_id: envelope.transition_id,
            metadata: { ...data },
          }
          if (planStatus) upsertOrchestrationPlan(plan)
          if (type === "plan.cancelling") {
            if (useChatStore.getState().agentStatus !== "cancelling") setAgentStatus("cancelling")
          } else if (type === "plan.cancelled") {
            const planId = text(data.plan_id)
            for (const task of useChatStore.getState().orchestrationTasks) {
              if (
                task.plan_id === planId
                && ["queued", "claimed", "running", "waiting_approval", "retry_wait", "cancel_requested"].includes(task.status)
              ) {
                upsertOrchestrationTask({
                  ...task,
                  status: "cancelled",
                  completed_at: occurredAt,
                  session_seq: sequence,
                  transition_id: envelope.transition_id,
                })
              }
            }
          }
        }
        if (type.startsWith("synthesis.")) {
          const planId = text(data.plan_id) || findPlanId(runId)
          if (planId) upsertOrchestrationPlan({ plan_id: planId, run_id: runId || undefined, status: type === "synthesis.started" ? "running" : "completed", synthesis_status: type === "synthesis.started" ? "running" : "completed", session_seq: sequence, transition_id: envelope.transition_id })
        }
        if (
          type === "task.queued" ||
          type === "task.started" ||
          type === "task.retrying" ||
          type === "task.cancelling" ||
          type === "task.waiting_approval" ||
          type === "task.completed" ||
          type === "task.failed" ||
          type === "task.cancelled"
        ) {
          const status: OrchestrationTask["status"] =
            type === "task.queued" ? "queued"
            : type === "task.started" ? "running"
            : type === "task.retrying" ? "retry_wait"
            : type === "task.cancelling" ? "cancel_requested"
            : type === "task.waiting_approval" ? "waiting_approval"
            : type === "task.completed" ? "completed"
            : type === "task.cancelled" ? "cancelled"
            : "failed"
          const taskId = text(data.task_id) || crypto.randomUUID()
          const planId = text(data.plan_id) || findPlanId(runId) || `plan:${runId}`
          upsertOrchestrationTask({
            plan_id: planId,
            task_id: taskId,
            run_id: runId || undefined,
            title: text(data.title) || text(data.objective) || "",
            status,
            objective: text(data.objective) || undefined,
            worker_type: text(data.worker_type) || undefined,
            parent_run_id: envelope.parent_run_id || text(data.parent_run_id) || null,
            execution_generation: number(data.execution_generation),
            attempt: number(data.attempt),
            worker_run_id: text(data.worker_run_id) || undefined,
            started_at: text(data.started_at) || (type === "task.started" ? occurredAt : undefined),
            completed_at: text(data.completed_at) || (["completed", "failed", "cancelled"].includes(status) ? occurredAt : undefined),
            duration_ms: number(data.duration_ms),
            result_summary: summarizeResult(data.result ?? data.output),
            dependencies: stringArray(data.dependencies),
            blocked_by: stringArray(data.blocked_by),
            approval_batch_id: text(data.approval_batch_id) || null,
            error: text(data.error) || errorDetail?.message || undefined,
            session_seq: sequence,
            transition_id: envelope.transition_id,
            metadata: { ...data },
          })
        }
        if (
          type === "llm.started"
          || type === "llm.completed"
          || type === "llm.validation_failed"
        ) {
          const updatedSteps = cacheBackgroundLlmStep(
            useChatStore.getState().steps,
            sessionId,
            envelope,
          )
          if (updatedSteps) setSteps(updatedSteps)
        }
        if (type === "llm.started") {
          if (useChatStore.getState().agentStatus !== "cancelling") setAgentStatus("thinking")
        }
        if (type === "tool.started") {
          const toolId = String(data.tool_call_id || crypto.randomUUID())
          const stepId = String(data.call_id || crypto.randomUUID())
          addStep({ id: stepId, session_id: sessionId, run_id: runId, parent_run_id: envelope.parent_run_id || text(data.parent_run_id) || null, step_number: useChatStore.getState().steps.length + 1, step_type: "tool_execution", status: "running", started_at: occurredAt, duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 })
          addToolCall({ id: toolId, tool_name: String(data.tool_name || "tool"), arguments: (data.arguments as Record<string, unknown>) || {}, status: "running", started_at: occurredAt, risk_level: (String(data.risk_level || "low") as ToolCall["risk_level"]), step_id: stepId, run_id: runId })
        } else if (type === "tool.completed") {
          const toolId = String(data.tool_call_id || "")
          const stepId = String(data.call_id || "")
          const status = data.status === "success" ? "success" : "failed"
          if (toolId) updateToolCall(toolId, { status, completed_at: occurredAt, duration_ms: Number(data.duration_ms || 0), output: data.output ? String(data.output) : undefined, error: data.error ? String(data.error) : undefined, error_detail: errorDetail || null })
          if (stepId) updateStep(stepId, { status: status === "success" ? "completed" : "failed", completed_at: occurredAt, duration_ms: Number(data.duration_ms || 0), error_message: data.error ? String(data.error) : null, error_detail: errorDetail || null })
        }
        if (type === "run.queued") {
          if (!setSessionRunStatus(sessionId, runId, "queued")) return
          const projected = summarizeSessionRuns(useChatStore.getState().sessionRunStatuses[sessionId] || {})
          if (projected.agentStatus === "queued") setAgentStatus("queued")
        } else if (type === "run.started" || type === "run.resumed") {
          if (!setSessionRunStatus(sessionId, runId, "running")) return
          if (useChatStore.getState().agentStatus !== "cancelling") setAgentStatus("running")
          updateSession(sessionId, { active_run_id: runId || null })
        } else if (type === "run.stopping") {
          if (!setSessionRunStatus(sessionId, runId, "stopping")) return
          setAgentStatus("cancelling")
          clearThinking()
        } else if (type === "run.cancelled") {
          if (!setSessionRunStatus(sessionId, runId, "cancelled")) return
          closeRunStreams(runId, "cancelled")
          const completedAt = occurredAt
          for (const entry of useChatStore.getState().executionTimeline) {
            if (entry.run_id !== runId || !["running", "waiting"].includes(entry.status)) continue
            const label = entry.event_type.startsWith("round.")
              ? "本轮任务已取消"
              : entry.event_type.startsWith("tool.")
                ? "工具操作已取消"
                : entry.event_type.startsWith("approval.")
                  ? "审批请求已取消"
                  : entry.event_type.startsWith("synthesis.")
                    ? "结果汇总已取消"
                    : entry.label
            upsertExecutionTimelineEntry({
              ...entry,
              event_type: entry.event_type.endsWith(".started")
                ? entry.event_type.replace(/\.started$/, ".cancelled")
                : entry.event_type,
              label,
              detail: [entry.detail, "运行已取消。"].filter(Boolean).join(" "),
              status: "cancelled",
              timestamp: completedAt,
            })
          }
          for (const step of useChatStore.getState().steps) {
            if (step.run_id === runId && step.status === "running") {
              updateStep(step.id, { status: "failed", completed_at: completedAt, error_message: "Stopped by user" })
            }
          }
          for (const toolCall of useChatStore.getState().toolCalls) {
            if (toolCall.run_id === runId && toolCall.status === "running") {
              updateToolCall(toolCall.id, { status: "failed", completed_at: completedAt, error: "Stopped by user" })
            }
          }
          const projected = summarizeSessionRuns(useChatStore.getState().sessionRunStatuses[sessionId] || {})
          setAgentStatus(projected.agentStatus === "idle" ? "idle" : projected.agentStatus)
          updateSession(sessionId, { active_run_id: projected.activeRunId })
          clearApprovals()
          clearThinking()
        }
        else if (type === "run.root_suspended") {
          // The root run can wait while workers continue. Clear transient
          // thinking state without presenting the whole plan as failed.
          clearThinking()
          if (useChatStore.getState().agentStatus !== "cancelling") setAgentStatus("running")
        }
        else if (type === "run.failed" || type === "run.budget_exceeded") {
          if (!setSessionRunStatus(sessionId, runId, "failed")) return
          closeRunStreams(runId, "failed")
          setAgentStatus("error")
          setErrorDetail(errorDetail || null)
          clearThinking()
          const phase = text(errorDetail?.phase) || text((data.error as Record<string, unknown> | undefined)?.phase)
          const hasCompletedPlan = useChatStore.getState().orchestrationPlans.some((plan) => plan.run_id === runId && plan.status === "completed")
          const errorMessage = phase === "answer_generation" && hasCompletedPlan
            ? "任务执行已完成，但总结生成失败"
            : phase === "answer_generation"
              ? "回答生成失败"
              : text(errorDetail?.message)
            || text((data.error as Record<string, unknown> | undefined)?.message)
            || text(data.message)
            || (type === "run.budget_exceeded" ? "运行已达到限制" : "任务未能完成")
          setError(errorMessage)
          const projected = summarizeSessionRuns(useChatStore.getState().sessionRunStatuses[sessionId] || {})
          if (projected.agentStatus !== "idle") setAgentStatus(projected.agentStatus)
          updateSession(sessionId, { active_run_id: projected.activeRunId })
        }
        else if (type === "run.completed") {
          if (!setSessionRunStatus(sessionId, runId, "completed")) return
          const projected = summarizeSessionRuns(useChatStore.getState().sessionRunStatuses[sessionId] || {})
          setAgentStatus(projected.agentStatus)
          updateSession(sessionId, { active_run_id: projected.activeRunId })
          clearThinking()
        }
      } catch { /* 单条事件格式错误不应中断 EventSource。 */ }
    }

    const attachListeners = (target: EventSource, generation: number) => {
      SESSION_EVENT_NAMES.forEach((name) => target.addEventListener(name, (event) => parse(event as MessageEvent, generation)))
    }
    const connect = (after?: number, targetStream?: string) => {
      currentSource?.close()
      connectionGeneration += 1
      const generation = connectionGeneration
      recoveryTarget = targetStream || null
      const targetUrl = sessionEventStreamUrl(apiBase, sessionId, after)
      currentSource = new EventSource(targetUrl, { withCredentials: true })
      sourceRef.current = currentSource
      currentSource.onopen = () => {
        if (!shouldHandleStreamCallback(disposed, generation, connectionGeneration)) return
        setConnectionStatus("connected")
        syncPendingApprovals()
        void syncRunLifecycle()
      }
      currentSource.onerror = () => {
        if (shouldHandleStreamCallback(disposed, generation, connectionGeneration)) setConnectionStatus("error")
      }
      attachListeners(currentSource, generation)
    }
    repairStream = (streamId, stream, runId) => {
      if (stream.repairAttempts >= 1) return
      const priorCursor = lastSeqRef.current
      const replayFrom = Math.max(0, (stream.startedSeq ?? 1) - 1)
      stream.repairAttempts += 1
      stream.content = ""
      stream.nextChunkId = 1
      stream.finalChunkId = null
      stream.pending.clear()
      stream.status = "streaming"
      streamsRef.current.set(streamId, stream)
      lastSeqRef.current = replayFrom
      processedSessionCursors.set(sessionId, replayFrom)
      backgroundReceivedCursors.set(sessionId, replayFrom)
      upsertAssistant(runId, "", stream.startedAt || new Date().toISOString(), streamId, `assistant:${runId}`, "streaming", 0)
      recoveryUntil = priorCursor
      connect(replayFrom, streamId)
    }
    const openInitialSource = async (knownEventCursor: number | undefined) => {
      let eventCursor = knownEventCursor
      if (eventCursor === undefined) {
        try {
          const history = await apiClient.getMessages(sessionId, 1)
          if (disposed) return
          eventCursor = history.snapshot_event_seq
          setSessionEventCursor(sessionId, eventCursor)
        } catch {
          if (!disposed) setConnectionStatus("error")
          return
        }
      }
      lastSeqRef.current = sessionEventResumeCursor(
        eventCursor,
        processedSessionCursors.get(sessionId) || 0,
        backgroundReceivedCursors.get(sessionId) || 0,
      )
      const initialUrl = sessionEventStreamUrl(
        apiBase,
        sessionId,
        lastSeqRef.current,
      )
      try {
        // 先取得终态 Run 快照，再建立可能从零重放的 SSE，避免旧 Run 事件复活 UI。
        const runs = await apiClient.listSessionRuns(sessionId)
        if (disposed) return
        const statuses = preserveTerminalRunStatuses(
          useChatStore.getState().sessionRunStatuses[sessionId] || {},
          Object.fromEntries(runs.map((run) => [run.run_id, run.status])),
        )
        replaceSessionRunStatuses(sessionId, statuses)
      } catch {
        // 状态查询不可用时仍订阅事件，依赖事件自身的单调终态保护。
      }
      if (disposed) return
      currentSource = new EventSource(initialUrl, { withCredentials: true })
      sourceRef.current = currentSource
      currentSource.onopen = onOpen
      currentSource.onerror = onError
      attachListeners(currentSource, connectionGeneration)
    }
    void openInitialSource(historyEventCursor)
    return () => {
      disposed = true
      currentSource?.close()
      for (const timer of orchestrationRefreshTimers.values()) clearTimeout(timer)
      orchestrationRefreshTimers.clear()
      if (sourceRef.current === currentSource) sourceRef.current = null
    }
  }, [sessionId, apiBase, setSessionEventCursor, setSteps, setConnectionStatus, setAgentStatus, addMessage, updateMessage, removeMessage, addStep, updateStep, addToolCall, updateToolCall, addApproval, mergeApprovals, resolveApproval, updateSession, setSessionRunStatus, replaceSessionRunStatuses, clearSteps, clearToolCalls, clearApprovals, clearThinking, setThinking, setError, setErrorDetail, clearErrorDetail, setAttachments, upsertOrchestrationTask, upsertOrchestrationPlan, beginOrchestrationSnapshotRequest, setOrchestrationSnapshot, upsertExecutionTimelineEntry, closeExecutionTimelineEntry, appendRunTimelineEvent])

  return sourceRef
}

/** 后台接收不活跃会话的 SSE，缓存事件供主订阅增量应用。 */
export function useBackgroundSessionEventStream(sessionId: string | null, apiBase: string) {
  useEffect(() => {
    if (!sessionId) return
    let source: EventSource | null = null
    let listeners: Array<readonly [string, (event: Event) => void]> = []
    let disposed = false
    const terminalTimers = new Set<ReturnType<typeof setTimeout>>()
    const releaseBackgroundSession = () => {
      void apiClient.listSessionRuns(sessionId).then((runs) => {
        if (disposed) return
        const active = runs.find((run) =>
          ["queued", "running", "stopping"].includes(run.status)
        )
        useChatStore.getState().updateSession(sessionId, {
          active_run_id: active?.run_id ?? null,
        })
      }).catch(() => undefined)
    }
    const maybeReleaseCompletedRun = (runId: string) => {
      const state = useChatStore.getState()
      const status = state.sessionRunStatuses[sessionId]?.[runId]
      const hasFinalAnswer = hasCompletedAssistant(
        state.sessionViewCache[sessionId]?.messages,
        runId,
      )
      if (!backgroundSessionCanRelease(status, hasFinalAnswer)) return
      const timer = setTimeout(() => {
        terminalTimers.delete(timer)
        releaseBackgroundSession()
      }, 0)
      terminalTimers.add(timer)
    }
    const connect = async () => {
      let baseline = sessionEventResumeCursor(
        undefined,
        processedSessionCursors.get(sessionId) || 0,
        backgroundReceivedCursors.get(sessionId) || 0,
      )
      const knownBaseline = useChatStore.getState().sessionEventCursors[sessionId]
      if (knownBaseline === undefined) {
        try {
          const history = await apiClient.getMessages(sessionId, 1)
          if (disposed) return
          baseline = sessionEventResumeCursor(
            history.snapshot_event_seq,
            baseline,
          )
          useChatStore.getState().setSessionEventCursor(
            sessionId,
            history.snapshot_event_seq,
          )
        } catch {
          return
        }
      } else {
        baseline = sessionEventResumeCursor(knownBaseline, baseline)
      }
      try {
        const runs = await apiClient.listSessionRuns(sessionId)
        if (disposed) return
        const statuses = preserveTerminalRunStatuses(
          useChatStore.getState().sessionRunStatuses[sessionId] || {},
          Object.fromEntries(runs.map((run) => [run.run_id, run.status])),
        )
        useChatStore.getState().replaceSessionRunStatuses(sessionId, statuses)
      } catch {
        // 保持已有状态并继续监听；SSE 事件仍会逐步补全 Run 状态。
      }
      if (disposed) return
      source = new EventSource(
        sessionEventStreamUrl(apiBase, sessionId, baseline),
        { withCredentials: true },
      )
      const connectedSource = source
      listeners = SESSION_EVENT_NAMES.map((name) => {
      const listener = (event: Event) => {
        const data = (event as MessageEvent<string>).data
        if (typeof data !== "string") return
        try {
          const envelope = JSON.parse(data) as ApplicationEventEnvelope
          if (typeof envelope.session_seq === "number" && envelope.session_seq <= (backgroundReceivedCursors.get(sessionId) || 0)) return
          cacheBackgroundEvent(sessionId, envelope)
          if (typeof envelope.session_seq === "number") {
            backgroundReceivedCursors.set(sessionId, Math.max(backgroundReceivedCursors.get(sessionId) || 0, envelope.session_seq))
          }
          const runId = envelope.run_id || ""
          const status = runStatusFromEvent(envelope.event_type || "")
          if (runId && status) {
            const state = useChatStore.getState()
            const projected = summarizeSessionRuns(state.sessionRunStatuses[sessionId] || {})
            useChatStore.setState((current) => ({
              sessionViewCache: {
                ...current.sessionViewCache,
                [sessionId]: { ...current.sessionViewCache[sessionId], agentStatus: projected.agentStatus },
              },
            }))
            maybeReleaseCompletedRun(runId)
          }
          if (envelope.event_type === "message.completed" && runId) {
            maybeReleaseCompletedRun(runId)
          }
        } catch {
          // 无法解析的后台事件留给重连时服务端重放，避免污染会话缓存。
          return
        }
      }
      connectedSource.addEventListener(name, listener)
      return [name, listener] as const
      })
    }
    void connect()
    return () => {
      disposed = true
      source?.close()
      for (const timer of terminalTimers) clearTimeout(timer)
      terminalTimers.clear()
      for (const [name, listener] of listeners) source?.removeEventListener(name, listener)
    }
  }, [sessionId, apiBase])
}

interface TimelineEventInput {
  type: string
  data: Record<string, unknown>
  envelope: ApplicationEventEnvelope
  runId: string
  timestamp: string
}

function projectTimelineEvent({ type, data, envelope, runId, timestamp }: TimelineEventInput): ExecutionTimelineEntry | null {
  const id = (suffix: string) => envelope.transition_id || `${suffix}:${envelope.session_seq ?? crypto.randomUUID()}`
  const errorValue = data.error
  const error = text(errorValue)
    || text((errorValue as Record<string, unknown> | undefined)?.message)
    || text((errorValue as Record<string, unknown> | undefined)?.code)
    || text((data.error_detail as Record<string, unknown> | undefined)?.message)
  const detail = error || undefined
  const base = (entry: Omit<ExecutionTimelineEntry, "id" | "timestamp" | "run_id" | "event_type" | "session_seq"> & { id?: string }): ExecutionTimelineEntry => ({
    id: entry.id || id(type),
    session_seq: envelope.session_seq,
    run_id: runId || undefined,
    event_type: type,
    timestamp,
    ...entry,
  })

  if (type.startsWith("thinking.")) return null

  if (type.startsWith("node.")) {
    const node = text(data.node || data.name)
    const label = nodeProgressLabel(node)
    if (!label) return null
    const status = type === "node.started" ? "running" : type === "node.failed" ? "failed" : "completed"
    const method = nodeProgressDetail(node)
    const nodeSummary = text(data.summary)
    const nodeDetail = status === "running"
      ? method
      : [method, status === "failed" ? error : nodeSummary ? `实际结果：${nodeSummary}` : ""].filter(Boolean).join("\n")
    return base({
      id: `progress:${text(data.activity_id) || `${runId}:${node}`}`,
      label: status === "running" ? label : status === "failed" ? `${label}未能完成` : `${label.replace(/^正在/, "")}已完成`,
      detail: nodeDetail || undefined,
      status,
      metadata: { node, duration_ms: number(data.duration_ms) },
    })
  }

  if (type === "retrieval.started" || type === "retrieval.completed" || type === "retrieval.failed") {
    const stage = text(data.stage)
    const status = type === "retrieval.started" ? "running" : type === "retrieval.failed" ? "failed" : "completed"
    const label = stage === "prefetch" ? "预检索相关资料" : "按需检索相关资料"
    const detail = status === "running"
      ? "正在查询本次请求可用的记忆、知识库或附件。"
      : status === "failed"
        ? error || "资料检索未能完成。"
        : `检索结束：${number(data.candidate_count) ?? 0} 个候选，${number(data.bundle_count) ?? 0} 条资料进入上下文。`
    return base({ id: `retrieval:${text(data.activity_id) || runId}`, label: status === "running" ? `正在${label}` : status === "failed" ? `${label}失败` : `${label}完成`, detail, status, metadata: { stage, provider: text(data.provider), candidate_count: number(data.candidate_count), bundle_count: number(data.bundle_count) } })
  }

  if (type.startsWith("retrieval.provider.")) {
    const provider = text(data.provider || data.name)
    const providerName = retrievalProviderLabel(provider)
    const statusValue = text(data.status)
    const status = type.endsWith(".started") || statusValue === "running"
      ? "running"
      : statusValue === "failed" || statusValue === "timeout" || type.endsWith(".failed")
        ? "failed"
        : statusValue === "skipped" || statusValue === "empty"
          ? "info"
          : "completed"
    const query = text(data.query)
    const sourceTitles = stringArray(data.source_titles) || []
    const detail = status === "running"
      ? [query ? `查询：${query}` : "正在执行检索。", data.resource_scope ? `范围：${JSON.stringify(data.resource_scope)}` : ""].filter(Boolean).join("\n")
      : status === "failed"
        ? [
            query ? `查询：${query}` : "",
            data.resource_scope ? `范围：${JSON.stringify(data.resource_scope)}` : "",
            error || (statusValue === "timeout" ? "检索超时。" : "检索未能完成。"),
          ].filter(Boolean).join("\n")
        : statusValue === "skipped"
          ? [
              query ? `原定查询：${query}` : "",
              error === "no_knowledge_bases_available" || error === "no_ready_knowledge_bases"
                ? "当前没有已就绪的知识库，因此跳过检索。"
                : error === "context_provider_unavailable" || error === "provider unavailable"
                  ? "当前环境未启用此检索通道，因此跳过检索。"
                  : error ? `检索已跳过：${error}` : "本轮未调用此检索通道。",
            ].filter(Boolean).join("\n")
          : statusValue === "empty"
            ? "没有找到相关结果。"
            : [query ? `查询：${query}` : "", `返回 ${number(data.result_count) ?? 0} 条结果，耗时 ${number(data.duration_ms) ?? 0} ms。`, sourceTitles.length ? `命中资料：${sourceTitles.join("、")}` : ""].filter(Boolean).join("\n")
    return base({
      id: `retrieval-provider:${text(data.activity_id) || `${runId}:${provider}`}`,
      label: status === "running" ? `正在查找${providerName}` : status === "failed" ? `${providerName}检索失败` : statusValue === "skipped" ? `未查询${providerName}` : `${providerName}检索完成`,
      detail,
      status,
      metadata: { provider, stage: text(data.stage), query: query || undefined, resource_scope: data.resource_scope, result_count: number(data.result_count), candidate_count: number(data.candidate_count), duration_ms: number(data.duration_ms), source_titles: sourceTitles },
    })
  }

  if (type === "retrieval.selection.completed") {
    const count = number(data.context_count) ?? 0
    return base({ id: `retrieval-selection:${text(data.activity_id) || runId}`, label: `已选择 ${count} 条资料用于回答`, detail: text(data.trace_status) === "complete" ? "资料选择记录已保存。" : "资料选择记录尚未完整保存。", status: text(data.trace_status) === "complete" ? "completed" : "info", metadata: { stage: text(data.stage), context_count: count } })
  }

  if (type === "run.started" || type === "run.resumed") return null
  if (type === "run.completed") return null
  if (type === "run.queued") return base({ id: `run:${runId}`, label: "任务已排队", detail: `会话内序号 ${number(data.sequence) ?? "未知"}，前序任务结束后开始执行。`, status: "waiting" })
  if (type === "run.stopping") return base({ id: `run:${runId}`, label: "正在停止任务", detail: "正在等待运行中的工作安全结束。", status: "waiting" })
  if (type === "run.cancelled") return base({ id: `run:${runId}`, label: "任务已取消", status: "info" })
  if (type === "run.root_suspended") return base({ id: `run:${runId}`, label: "主任务等待子任务继续", detail: "并行任务仍在处理，完成后将继续汇总结果。", status: "waiting" })
  if (type === "run.failed" || type === "run.budget_exceeded") return base({ id: `run:${runId}`, label: type === "run.budget_exceeded" ? "任务已达到运行限制" : "任务未能完成", detail, status: "failed" })

  if (type === "llm.validation_failed") {
    return projectLlmValidationFailureTimeline(envelope, runId, timestamp)
  }
  if (type.startsWith("llm.")) return null

  if (type === "tool.started") {
    const toolName = text(data.tool_name)
    const argsSummary = (data.arguments_summary as Record<string, unknown> | undefined) || {}
    const subject = text(argsSummary.filename || argsSummary.file_name || argsSummary.path || argsSummary.file_path || argsSummary.title || argsSummary.query || argsSummary.url || argsSummary.command)
    return base({
      id: `tool:${text(data.tool_call_id)}:started`,
      label: subject ? `${toolActionLabel(toolName, "started")}：${subject}` : toolActionLabel(toolName, "started"),
      detail: Object.entries(argsSummary).map(([key, value]) => `${key}：${String(value)}`).join("\n") || undefined,
      status: "info",
      metadata: { ...(toolName ? { tool_name: toolName } : {}), ...(Object.keys(argsSummary).length ? { arguments_summary: argsSummary } : {}) },
    })
  }
  if (type === "tool.completed") {
    const toolName = text(data.tool_name)
    const failed = Boolean(error) || ["failed", "denied", "timeout", "cancelled"].includes(text(data.status))
    return base({
      id: `tool:${text(data.tool_call_id)}:completed`,
      label: toolActionLabel(toolName, failed ? "failed" : "completed"),
      detail: error || `工具已返回结果${number(data.duration_ms) != null ? `，耗时 ${number(data.duration_ms)} ms` : ""}${number(data.output_length) != null ? `，结果约 ${number(data.output_length)} 个字符` : ""}。`,
      status: failed ? "failed" : "completed",
      metadata: { ...(toolName ? { tool_name: toolName } : {}), ...(number(data.duration_ms) != null ? { duration_ms: number(data.duration_ms) } : {}), ...(number(data.output_length) != null ? { output_length: number(data.output_length) } : {}), ...(text(data.output_preview) ? { output_preview: text(data.output_preview) } : {}) },
    })
  }

  if (type === "approval.required") {
    const toolName = text(data.tool_name)
    const args = (data.arguments as Record<string, unknown> | undefined) || {}
    return base({ id: `approval:${text(data.approval_id)}`, label: "需要你确认这项操作", detail: approvalActionDescription(toolName, args), status: "waiting", metadata: { ...(toolName ? { tool_name: toolName } : {}), ...(data.arguments ? { arguments: data.arguments } : {}) } })
  }
  if (type === "approval.resolved") {
    const decision = text(data.decision)
    const detail = decision === "approved" ? "你已允许此操作继续。" : decision === "cancelled" ? "此操作已取消，未继续执行。" : decision === "expired" ? "审批已到期，操作已拒绝且未执行。" : "你已拒绝此操作，未继续执行。"
    return base({ id: `approval:${text(data.approval_id)}`, label: decision === "expired" ? "审批已到期" : "确认已提交", detail, status: decision === "expired" ? "failed" : "completed" })
  }

  if (type.startsWith("plan.")) {
    const planId = text(data.plan_id)
    const labels: Record<string, string> = { "plan.accepted": "执行计划已受理", "plan.created": "已制定执行计划", "plan.cancelling": "正在停止执行计划", "plan.revised": "执行计划已调整", "plan.completed": "执行计划已完成", "plan.failed": "执行计划未能完成", "plan.cancelled": "执行计划已取消" }
    const status = type === "plan.failed" ? "failed" : type === "plan.created" || type === "plan.revised" ? "running" : type === "plan.accepted" || type === "plan.cancelled" ? "info" : type === "plan.cancelling" ? "waiting" : "completed"
    const id = type === "plan.accepted" ? `plan-accepted:${planId}` : `plan:${planId}`
    return base({ id, label: labels[type] || "执行计划已更新", detail: error || undefined, status })
  }
  if (type === "round.started" || type === "round.completed") {
    const roundId = text(data.round_id)
    const taskIds = stringArray(data.task_ids) || []
    const version = number(data.plan_version)
    const terminalCount = number(data.terminal_task_count)
    const expectedCount = number(data.expected_task_count)
    const detail = type === "round.started"
      ? [taskIds.length ? `${taskIds.length} 个任务进入本轮执行` : "本轮任务开始执行", version ? `计划版本 v${version}` : ""].filter(Boolean).join(" · ")
      : terminalCount !== undefined && expectedCount !== undefined
        ? `${terminalCount} / ${expectedCount} 个任务已结束`
        : undefined
    return base({
      id: `round:${roundId}`,
      label: type === "round.started" ? "开始执行一轮任务" : "本轮任务已处理完毕",
      detail,
      status: type === "round.started" ? "running" : "completed",
      metadata: {
        ...(text(data.plan_id) ? { plan_id: text(data.plan_id) } : {}),
        ...(roundId ? { round_id: roundId } : {}),
        ...(version ? { plan_version: version } : {}),
        ...(taskIds.length ? { task_ids: taskIds } : {}),
      },
    })
  }
  if (type.startsWith("task.")) {
    const taskId = text(data.task_id)
    if (!taskId) return null
    const title = text(data.title || data.objective) || "执行任务"
    const workerName = text(data.worker_name)
    const status = type === "task.started" ? "running"
      : type === "task.waiting_approval" || type === "task.cancelling" ? "waiting"
        : type === "task.failed" ? "failed"
          : type === "task.cancelled" ? "cancelled"
            : type === "task.completed" ? "completed" : "info"
    const statusCopy: Record<string, string> = {
      "task.queued": "已加入执行计划",
      "task.started": workerName ? `已分配给${workerName}，准备执行：${title}` : `任务已开始：${title}`,
      "task.retrying": workerName ? `${workerName}执行遇到问题，准备重试：${title}` : `准备重试：${title}`,
      "task.waiting_approval": `等待确认：${title}`,
      "task.completed": workerName ? `${workerName}执行成功：${title}` : `已完成：${title}`,
      "task.failed": workerName ? `${workerName}执行失败：${title}` : `未能完成：${title}`,
      "task.cancelled": workerName ? `${workerName}执行已取消：${title}` : `已取消：${title}`,
      "task.cancelling": workerName ? `正在请求${workerName}停止：${title}` : `正在请求停止任务：${title}`,
    }
    const result = workerResultText(data.result ?? data.output ?? data.result_summary, text(data.result_ref), data.result_available_in_final === true)
    const taskError = text(data.error) || text((data.error as Record<string, unknown> | undefined)?.message)
    const objective = text(data.objective)
    const roundNumber = number(data.round_number ?? data.plan_version)
    const parallelTaskCount = number(data.parallel_task_count)
    const detail = [
      roundNumber ? `第 ${roundNumber} 轮${parallelTaskCount && parallelTaskCount > 1 ? `（${parallelTaskCount} 项并行任务）` : ""}` : "",
      objective ? `任务目标：${objective}` : "",
      taskError ? `执行错误：${taskError}` : "",
      result ? `执行结果：\n${result}` : "",
    ].filter(Boolean).join("\n")
    return base({
      id: `task:${text(data.plan_id)}:${taskId}:${text(data.execution_id) || number(data.attempt) || "task"}:${type}`,
      label: statusCopy[type] || title,
      detail: detail || undefined,
      status,
      metadata: { task_id: taskId, plan_id: text(data.plan_id), worker_name: workerName || undefined, worker_type: text(data.worker_type) || undefined, attempt: number(data.attempt), round_number: roundNumber, parallel_task_count: parallelTaskCount },
    })
  }
  if (type.startsWith("synthesis.")) return base({ id: `synthesis:${text(data.plan_id)}`, label: type === "synthesis.started" ? "正在汇总处理结果" : "处理结果已汇总", status: type === "synthesis.started" ? "running" : "completed" })

  if (type === "agent.waiting_for_files") return base({ id: `files:${runId}`, label: "等待文件处理完成后继续", status: "waiting" })
  if (type.startsWith("file_processing")) {
    const attachmentId = text(data.attachment_id)
    const status = type.endsWith("failed") ? "failed" : type.endsWith("completed") ? "completed" : "running"
    const label = type.endsWith("failed") ? "文件处理失败" : type.endsWith("completed") ? "文件已处理完成" : "正在处理文件"
    return base({ id: `file:${attachmentId}`, label, detail, status })
  }
  if (type === "attachment_updated") return base({ id: `attachment:${text(data.attachment_id)}`, label: "附件状态已更新", status: "info" })

  return null
}

function text(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function number(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value)
  return undefined
}

function nodeProgressLabel(node: string): string | null {
  const labels: Record<string, string> = {
    prepare_request: "正在整理请求",
    prefetch_context: "正在预检索相关资料",
    decide_root_action: "正在判断处理方式",
    acquire_context: "正在查找相关资料",
    redecide_root_action: "正在依据资料确认处理方式",
    generate_plan: "正在制定执行计划",
    materialize_execution_plan: "正在安排执行任务",
    build_plan_response: "正在整理任务结果",
    generate_answer: "正在组织回答",
    assemble_final_response: "正在完成本次请求",
  }
  return labels[node] || null
}

function nodeProgressDetail(node: string): string {
  const details: Record<string, string> = {
    prepare_request: "核对本轮消息、关联附件和可用资源。",
    prefetch_context: "用原始问题并行查询长期记忆与已就绪知识库。",
    decide_root_action: "根据用户目标、现有资料和可用工具选择处理路径。",
    acquire_context: "按 Root 提交的查询和资源范围获取资料。",
    redecide_root_action: "结合刚获取的资料重新判断下一步处理方式。",
    generate_plan: "根据目标与资料生成可执行的任务草稿。",
    materialize_execution_plan: "校验任务依赖和工具范围，再提交执行计划。",
    build_plan_response: "整理已完成任务的结果，准备生成最终答复。",
    generate_answer: "根据本轮资料和执行结果生成最终答复。",
    assemble_final_response: "核对本轮最终状态并完成响应。",
  }
  return details[node] || "正在执行当前处理阶段。"
}

function retrievalProviderLabel(provider: string): string {
  const labels: Record<string, string> = {
    memory: "长期记忆",
    knowledge: "知识库",
    file: "当前附件",
    graph: "关联资料",
    conversation: "对话历史",
  }
  return labels[provider] || provider || "资料"
}

function stringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const items = value.map((item) => typeof item === "string" ? item : "").filter(Boolean)
  return items.length > 0 ? items : undefined
}

function findPlanId(runId: string): string | undefined {
  if (!runId) return undefined
  return useChatStore.getState().orchestrationPlans.find((plan) => plan.run_id === runId)?.plan_id
}
