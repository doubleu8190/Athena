import type {
  SessionSnapshotPayload,
  SessionTurnSnapshot,
} from "../types/events"
import type {
  AgentStatus,
  ExecutionError,
  ExecutionTimelineEntry,
  Message,
} from "../types/index"

export interface SessionSnapshotView {
  messages?: Message[]
  executionTimeline?: ExecutionTimelineEntry[]
  agentStatus?: AgentStatus
  error?: string | null
  errorDetail?: ExecutionError | null
}

function upsertMessage(messages: Message[], incoming: Message): Message[] {
  const index = messages.findIndex((message) => (
    message.id === incoming.id
    || (
      message.role === incoming.role
      && Boolean(message.run_id)
      && message.run_id === incoming.run_id
    )
  ))
  if (index < 0) return [...messages, incoming].sort(compareMessages)
  const existing = messages[index]
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

function compareTimeline(
  left: ExecutionTimelineEntry,
  right: ExecutionTimelineEntry,
): number {
  return left.timestamp.localeCompare(right.timestamp)
    || left.id.localeCompare(right.id)
}

function upsertTimeline(
  timeline: ExecutionTimelineEntry[],
  incoming: ExecutionTimelineEntry,
): ExecutionTimelineEntry[] {
  const index = timeline.findIndex((entry) => entry.id === incoming.id)
  if (index < 0) return [...timeline, incoming].sort(compareTimeline)
  if (timeline[index].status === "completed" && incoming.status !== "completed") {
    return timeline
  }
  const next = [...timeline]
  next[index] = { ...timeline[index], ...incoming }
  return next.sort(compareTimeline)
}

function applyTurn(
  view: SessionSnapshotView,
  payload: SessionTurnSnapshot,
): SessionSnapshotView {
  let messages = [...(view.messages || [])]
  if (payload.user) {
    messages = upsertMessage(messages, {
      id: payload.user.message_id,
      role: "user",
      content: payload.user.content,
      timestamp: payload.user.time,
      run_id: payload.run_id,
    })
  }
  if (payload.assistant) {
    messages = upsertMessage(messages, {
      id: payload.assistant.message_id,
      role: "assistant",
      content: payload.assistant.content,
      timestamp: payload.assistant.time,
      run_id: payload.run_id,
      stream_status: payload.assistant.completed ? "completed" : "streaming",
      last_chunk_id: payload.assistant.last_chunk_id,
    })
  }
  if (
    !payload.assistant
    && (payload.run_status === "failed" || payload.run_status === "cancelled")
  ) {
    // 终态失败或取消不会持久化半截回答；清除此前由 SSE 累积的临时正文。
    messages = messages.filter((message) => !(
      message.role === "assistant"
      && message.run_id === payload.run_id
      && message.stream_status === "streaming"
    ))
  }

  let timeline = [...(view.executionTimeline || [])]
  for (const item of payload.root_process) {
    timeline = upsertTimeline(timeline, progressEntry(
      item,
      payload.run_id,
      "root",
    ))
  }
  for (const task of payload.tasks) {
    for (const item of task.process) {
      timeline = upsertTimeline(timeline, progressEntry(
        item,
        payload.run_id,
        "worker",
        task.task_title,
        task.worker_name,
      ))
    }
    if (task.result) {
      timeline = upsertTimeline(timeline, {
        id: `task-result:${payload.run_id}:${task.execution_id}`,
        run_id: payload.run_id,
        event_type: "task.result",
        label: task.task_title,
        detail: task.result.content,
        status: task.status.includes("fail")
          ? "failed"
          : task.status.includes("cancel")
            ? "cancelled"
            : "completed",
        timestamp: task.result.time,
        metadata: {
          worker_name: task.worker_name,
          task_title: task.task_title,
        },
      })
    }
  }

  const runStatus = payload.run_status
  let agentStatus = view.agentStatus || "idle"
  let error = view.error ?? null
  let errorDetail = view.errorDetail ?? null
  if (["queued", "running", "stopping", "waiting"].includes(runStatus)) {
    agentStatus = "running"
    error = null
    errorDetail = null
  } else if (runStatus === "completed") {
    agentStatus = "completed"
    error = null
    errorDetail = null
  } else if (runStatus === "failed" || runStatus === "budget_exceeded") {
    agentStatus = "error"
    error = "任务未能完成"
    errorDetail = {
      code: runStatus === "budget_exceeded" ? "run_budget_exceeded" : "run_failed",
      message: "任务未能完成",
      retryable: false,
    }
  } else if (runStatus === "cancelled" || runStatus === "idle") {
    agentStatus = "idle"
    error = null
    errorDetail = null
  }
  return { ...view, messages, executionTimeline: timeline, agentStatus, error, errorDetail }
}

function progressEntry(
  item: { id?: string; progress_id?: string; time: string; content: string },
  runId: string,
  source: "root" | "worker",
  taskTitle?: string,
  workerName?: string,
): ExecutionTimelineEntry {
  const sourceLabel = source === "root"
    ? "Root"
    : workerName || "Worker"
  return {
    id: `progress:${item.progress_id || item.id}`,
    run_id: runId,
    event_type: `progress.${source}`,
    label: item.content,
    detail: taskTitle ? `${taskTitle} · ${sourceLabel}` : sourceLabel,
    status: "info",
    timestamp: item.time,
  }
}

/** 合并多轮 SSE 快照，保留现有视图并按稳定身份去重。 */
export function applySessionSnapshot(
  view: SessionSnapshotView,
  payload: SessionSnapshotPayload,
): SessionSnapshotView {
  return payload.turns.reduce(applyTurn, view)
}
