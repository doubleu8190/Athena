import { upsertTimelineEntry } from "./executionTimeline.ts"
import type { ApplicationEventEnvelope } from "../types/events.ts"
import type {
  ExecutionTimelineEntry,
  Message,
  RunTimelineEvent,
  SessionHistoryTurn,
  Step,
} from "../types/index.ts"

type SessionTimelineCache = {
  runTimelineEvents?: RunTimelineEvent[]
  executionTimeline?: ExecutionTimelineEntry[]
  executionTimelineTerminalMarkers?: Record<string, ExecutionTimelineEntry["status"]>
}

const LLM_STEP_EVENTS = new Set([
  "llm.started",
  "llm.completed",
  "llm.validation_failed",
])

export function historyMessages(turns: SessionHistoryTurn[], sessionId?: string): Message[] {
  return turns.flatMap((turn) => [
    {
      id: turn.user.id || `history:user:${turn.run_id}`,
      role: "user" as const,
      content: turn.user.content,
      timestamp: turn.user.time,
      session_id: sessionId,
      run_id: turn.run_id,
    },
    ...(turn.assistant ? [{
      id: `assistant:${turn.run_id}`,
      role: "assistant" as const,
      content: turn.assistant.content,
      timestamp: turn.assistant.time,
      session_id: sessionId,
      run_id: turn.run_id,
      stream_status: "completed" as const,
    }] : []),
  ])
}

export function historyTimeline(turns: SessionHistoryTurn[]): ExecutionTimelineEntry[] {
  const entries: ExecutionTimelineEntry[] = []
  for (const turn of turns) {
    turn.root_process.forEach((item, index) => entries.push({
      id: `history:${turn.run_id}:root:${index}`,
      run_id: turn.run_id,
      event_type: "history.root",
      label: item.content,
      status: "info",
      timestamp: item.time,
      session_seq: item.session_seq ?? undefined,
    }))
    turn.tasks.forEach((task, taskIndex) => {
      const terminal = task.content
        ? task.process.find((item) => item.session_seq === task.session_seq)
        : undefined
      task.process.forEach((item, index) => {
        if (task.content && item.session_seq === task.session_seq) return
        entries.push({
          id: `history:${turn.run_id}:task:${taskIndex}:${index}`,
          run_id: turn.run_id,
          event_type: "history.worker",
          label: item.content,
          status: "info",
          timestamp: item.time,
          session_seq: item.session_seq ?? undefined,
        })
      })
      if (task.content) entries.push({
        id: `history:${turn.run_id}:task:${taskIndex}:result`,
        run_id: turn.run_id,
        event_type: "history.worker.result",
        label: terminal?.content || "Worker 执行结果",
        detail: task.content,
        status: "completed",
        timestamp: task.time,
        session_seq: task.session_seq ?? undefined,
      })
    })
  }
  return entries.sort((left, right) => left.timestamp.localeCompare(right.timestamp))
}

export function filterChatTimelineForLoadedRuns(
  entries: ExecutionTimelineEntry[],
  messages: Message[],
  activeRunIds: Iterable<string>,
): ExecutionTimelineEntry[] {
  const loadedRunIds = new Set(
    messages
      .map((item) => item.run_id)
      .filter((runId): runId is string => Boolean(runId)),
  )
  const activeRunIdSet = new Set(activeRunIds)
  return entries.filter((entry) => {
    const belongsToLoadedRun = !entry.run_id
      || loadedRunIds.has(entry.run_id)
      || activeRunIdSet.has(entry.run_id)
    return belongsToLoadedRun
      && !entry.event_type.startsWith("node.")
      && entry.event_type !== "run.queued"
      && (!entry.event_type.startsWith("retrieval.") || entry.status === "failed")
  })
}

export function projectLlmValidationFailureTimeline(
  envelope: ApplicationEventEnvelope,
  runId: string,
  timestamp: string,
): ExecutionTimelineEntry {
  const payload = envelope.payload || {}
  const rawError = payload.error ?? envelope.error_detail
  const errorRecord = rawError && typeof rawError === "object"
    ? rawError as Record<string, unknown>
    : undefined
  const error = typeof rawError === "string"
    ? rawError
    : typeof errorRecord?.message === "string"
      ? errorRecord.message
      : typeof errorRecord?.code === "string"
        ? errorRecord.code
        : ""
  const callId = typeof payload.call_id === "string" ? payload.call_id : ""
  return {
    id: `llm-validation:${callId || envelope.session_seq || runId}`,
    run_id: runId,
    event_type: "llm.validation_failed",
    label: "模型输出校验失败",
    detail: error || "模型返回内容未通过校验。",
    status: "failed",
    timestamp,
    session_seq: envelope.session_seq,
    metadata: {
      call_id: callId || undefined,
      parent_activity_id: typeof payload.parent_activity_id === "string"
        ? payload.parent_activity_id
        : undefined,
    },
  }
}

export function cacheSessionTimelineProjection(
  previous: SessionTimelineCache,
  envelope: ApplicationEventEnvelope,
  timeline: ExecutionTimelineEntry | null,
): SessionTimelineCache {
  const next: SessionTimelineCache = {}
  const sequence = envelope.session_seq
  const runId = envelope.run_id || ""
  if (typeof sequence === "number" && runId) {
    const events = previous.runTimelineEvents || []
    if (!events.some((item) => item.session_seq === sequence)) {
      next.runTimelineEvents = [...events, {
        session_seq: sequence,
        event_type: envelope.event_type || "",
        occurred_at: envelope.occurred_at || new Date().toISOString(),
        run_id: runId,
        payload: envelope.payload || {},
      }].sort((left, right) => left.session_seq - right.session_seq)
    }
  }
  if (timeline) {
    next.executionTimeline = upsertTimelineEntry(
      previous.executionTimeline || [],
      previous.executionTimelineTerminalMarkers || {},
      timeline,
    )
  }
  return next
}

export function cacheBackgroundLlmStep(
  previousSteps: Step[] | undefined,
  sessionId: string,
  envelope: ApplicationEventEnvelope,
): Step[] | undefined {
  const type = envelope.event_type || ""
  if (!LLM_STEP_EVENTS.has(type)) return undefined
  const payload = envelope.payload || {}
  const runId = envelope.run_id || ""
  const callId = typeof payload.call_id === "string" && payload.call_id
    ? payload.call_id
    : `llm:${runId}:${envelope.session_seq ?? envelope.occurred_at ?? "unknown"}`
  const steps = [...(previousSteps || [])]
  const existingIndex = steps.findIndex((step) =>
    step.id === callId && step.step_type === "llm_call"
  )
  const existing = existingIndex >= 0 ? steps[existingIndex] : undefined
  if (existing?.status === "failed" && type !== "llm.validation_failed") return steps

  const timestamp = envelope.occurred_at || new Date().toISOString()
  const payloadError = payload.error
  const errorMessage = typeof payloadError === "string"
    ? payloadError
    : payloadError && typeof payloadError === "object"
      && typeof (payloadError as Record<string, unknown>).message === "string"
      ? String((payloadError as Record<string, unknown>).message)
      : envelope.error_detail?.message
        || (type === "llm.validation_failed" ? "模型输出校验失败" : null)
  const payloadErrorDetail = payload.error_detail
  const errorDetail = payloadErrorDetail && typeof payloadErrorDetail === "object"
    ? payloadErrorDetail as Step["error_detail"]
    : envelope.error_detail || null
  const status: Step["status"] = type === "llm.started"
    ? "running"
    : type === "llm.validation_failed"
      || payload.status === "failed"
      ? "failed"
      : "completed"
  const startedAt = existing?.started_at
    || (typeof payload.started_at === "string" ? payload.started_at : timestamp)
  const durationMs = typeof payload.duration_ms === "number"
    ? payload.duration_ms
    : Number(payload.duration_ms || existing?.duration_ms || 0)
  const nextStep: Step = {
    id: callId,
    session_id: sessionId,
    run_id: runId,
    parent_run_id: envelope.parent_run_id
      || (typeof payload.parent_run_id === "string" ? payload.parent_run_id : null),
    step_number: existing?.step_number ?? steps.length + 1,
    step_type: "llm_call",
    status,
    started_at: startedAt,
    completed_at: type === "llm.started" ? existing?.completed_at ?? null : timestamp,
    duration_ms: durationMs,
    llm_input_tokens: typeof payload.llm_input_tokens === "number"
      ? payload.llm_input_tokens
      : existing?.llm_input_tokens ?? 0,
    llm_output_tokens: typeof payload.llm_output_tokens === "number"
      ? payload.llm_output_tokens
      : existing?.llm_output_tokens ?? 0,
    error_message: type === "llm.started" ? existing?.error_message ?? null : errorMessage,
    error_detail: type === "llm.started" ? existing?.error_detail ?? null : errorDetail,
  }
  if (existingIndex >= 0) steps[existingIndex] = { ...existing, ...nextStep }
  else steps.push(nextStep)
  return steps
}

export function mergeHistoryMessages(
  persisted: Message[],
  live: Message[],
): Message[] {
  const merged = new Map<string, Message>()
  const semanticKeys = new Map<string, string>()
  const add = (message: Message, authoritative: boolean) => {
    const semanticKey = message.run_id
      ? `${message.role}:${message.run_id}`
      : `id:${message.id}`
    const existingId = semanticKeys.get(semanticKey)
    if (!existingId) {
      merged.set(message.id, message)
      semanticKeys.set(semanticKey, message.id)
      return
    }
    const existing = merged.get(existingId)
    if (!existing) {
      merged.set(message.id, message)
      semanticKeys.set(semanticKey, message.id)
      return
    }
    const next = authoritative
      ? { ...existing, ...message }
      : { ...message, ...existing }
    merged.delete(existingId)
    merged.set(next.id, next)
    semanticKeys.set(semanticKey, next.id)
  }
  persisted.forEach((message) => add(message, true))
  live.forEach((message) => add(message, false))
  return [...merged.values()].sort((left, right) => {
    const delta = Date.parse(left.timestamp) - Date.parse(right.timestamp)
    return Number.isNaN(delta) || delta === 0
      ? left.id.localeCompare(right.id)
      : delta
  })
}

export function hasCompletedAssistant(
  messages: Message[] | undefined,
  runId: string,
): boolean {
  return Boolean(messages?.some((message) =>
    message.role === "assistant"
    && message.run_id === runId
    && message.stream_status === "completed",
  ))
}

export function sessionEventResumeCursor(
  snapshotEventSeq: number | undefined,
  processedCursor = 0,
  backgroundCursor = 0,
): number {
  const receivedCursor = Math.max(0, processedCursor, backgroundCursor)
  return receivedCursor > 0
    ? receivedCursor
    : Math.max(0, snapshotEventSeq ?? 0)
}

export function backgroundSessionCanRelease(
  runStatus: string | undefined,
  hasFinalAnswer: boolean,
): boolean {
  if (runStatus === "failed" || runStatus === "cancelled") return true
  return runStatus === "completed" && hasFinalAnswer
}

export function sessionEventStreamUrl(
  apiBase: string,
  sessionId: string,
  after?: number,
): string {
  const url = `${apiBase.replace(/\/$/, "")}/api/sessions/${encodeURIComponent(sessionId)}/events`
  return after !== undefined && after > 0 ? `${url}?after=${after}` : url
}
