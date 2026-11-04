import type {
  ExecutionTimelineEntry,
  ExecutionTimelineStatus,
  Message,
  SessionHistoryContent,
  SessionHistoryTurn,
} from "../types/index.ts"

/** 为没有后端 ID 的历史内容生成跨分页稳定的本地标识。 */
function historyId(kind: string, ...values: string[]): string {
  let hash = 2166136261
  const input = values.join("\u0000")
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return `history:${kind}:${(hash >>> 0).toString(36)}`
}

function contentId(role: "user" | "assistant", value: SessionHistoryContent): string {
  return historyId(role, value.time, value.content)
}

/** 将后端任务终态映射为历史时间线状态；旧响应缺失状态时保持安全默认。 */
function historyTaskStatus(status?: string): ExecutionTimelineStatus {
  const normalized = status?.toLowerCase()
  if (normalized === "failed" || normalized === "timed_out") return "failed"
  if (normalized === "cancelled" || normalized === "superseded") return "cancelled"
  return "completed"
}

/** 把精简历史轮次转换为聊天消息；后端内部身份不会进入前端消息模型。 */
export function historyMessages(turns: SessionHistoryTurn[], sessionId?: string): Message[] {
  const messages: Message[] = []
  for (const turn of turns) {
    messages.push({
      id: contentId("user", turn.user),
      role: "user",
      content: turn.user.content,
      timestamp: turn.user.time,
      session_id: sessionId,
    })
    if (turn.assistant) {
      messages.push({
        id: contentId("assistant", turn.assistant),
        role: "assistant",
        content: turn.assistant.content,
        timestamp: turn.assistant.time,
        session_id: sessionId,
        stream_status: "completed",
      })
    }
  }
  return messages
}

/** 把 Root/Worker Progress 和任务结果投影为统一的用户可见时间线。 */
export function historyTimeline(turns: SessionHistoryTurn[]): ExecutionTimelineEntry[] {
  const entries: ExecutionTimelineEntry[] = []
  for (const turn of turns) {
    const turnId = historyId("turn", turn.user.time, turn.user.content)
    turn.root_process.forEach((item, index) => {
      entries.push({
        id: historyId("root", turnId, item.time, item.content, String(index)),
        event_type: "history.root",
        label: item.content,
        status: "info",
        timestamp: item.time,
        metadata: { turn_id: turnId },
      })
    })
    turn.tasks.forEach((task, taskIndex) => {
      const taskId = historyId(
        "task",
        turnId,
        task.worker_name,
        task.task_title,
        String(taskIndex),
      )
      task.process.forEach((item, index) => {
        entries.push({
          id: historyId("worker", taskId, item.time, item.content, String(index)),
          event_type: "history.worker",
          label: item.content,
          detail: `${task.worker_name} · ${task.task_title}`,
          status: "info",
          timestamp: item.time,
          metadata: {
            turn_id: turnId,
            worker_name: task.worker_name,
            task_title: task.task_title,
          },
        })
      })
      if (task.result) {
        entries.push({
          id: historyId("worker-result", taskId),
          event_type: "history.worker.result",
          label: task.task_title,
          detail: task.result.content,
          status: historyTaskStatus(task.status),
          timestamp: task.result.time,
          metadata: {
            turn_id: turnId,
            worker_name: task.worker_name,
            task_title: task.task_title,
          },
        })
      }
    })
  }
  return entries.sort((left, right) => (
    left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id)
  ))
}

/** 合并同一条本地历史消息与实时快照，保留已经落库的最终正文。 */
export function mergeHistoryMessages(
  persisted: Message[],
  live: Message[],
): Message[] {
  const merged = [...persisted]
  for (const candidate of live) {
    const existingIndex = merged.findIndex((item) => (
      item.id === candidate.id
      || (
        item.role === candidate.role
        && item.timestamp === candidate.timestamp
        && item.content === candidate.content
      )
      || (
        item.role === candidate.role
        && Boolean(item.run_id)
        && item.run_id === candidate.run_id
      )
    ))
    if (existingIndex < 0) {
      merged.push(candidate)
      continue
    }
    const existing = merged[existingIndex]
    merged[existingIndex] = existing.stream_status === "completed"
      ? existing
      : { ...candidate, ...existing, id: existing.id }
  }
  return merged.sort((left, right) => {
    const timeDelta = Date.parse(left.timestamp) - Date.parse(right.timestamp)
    return Number.isFinite(timeDelta) && timeDelta !== 0
      ? timeDelta
      : left.id.localeCompare(right.id)
  })
}

/** 判断历史投影与实时投影是否表示同一条可见执行事实。 */
export function timelineEntriesMatch(
  left: ExecutionTimelineEntry,
  right: ExecutionTimelineEntry,
): boolean {
  if (left.run_id && right.run_id && left.run_id !== right.run_id) return false
  const leftResult = left.event_type === "task.result"
    || left.event_type === "history.worker.result"
  const rightResult = right.event_type === "task.result"
    || right.event_type === "history.worker.result"
  return leftResult === rightResult
    && left.timestamp === right.timestamp
    && left.label === right.label
}

/** 按稳定本地 ID 合并历史时间线和实时可见进度。 */
export function mergeHistoryTimeline(
  persisted: ExecutionTimelineEntry[],
  live: ExecutionTimelineEntry[],
): ExecutionTimelineEntry[] {
  const merged = [...persisted]
  for (const entry of live) {
    const index = merged.findIndex((item) => (
      item.id === entry.id || timelineEntriesMatch(item, entry)
    ))
    if (index < 0) merged.push(entry)
    else merged[index] = { ...merged[index], ...entry }
  }
  return merged.sort((left, right) => (
    left.timestamp.localeCompare(right.timestamp) || left.id.localeCompare(right.id)
  ))
}

/** 只展示已经加载的用户轮次和当前活动 Run，隔离历史分页之外的实时缓存。 */
export function filterChatTimelineForLoadedRuns(
  entries: ExecutionTimelineEntry[],
  messages: Message[],
  activeRunIds: string[],
): ExecutionTimelineEntry[] {
  const visibleRunIds = new Set([
    ...messages.flatMap((message) => message.run_id ? [message.run_id] : []),
    ...activeRunIds,
  ])
  return entries.filter((entry) => (
    !entry.run_id || visibleRunIds.has(entry.run_id)
  ))
}

/** 读取最新历史页后建立聊天 SSE；后续分页不得改变该连接基线。 */
export function sessionEventStreamUrl(
  apiBase: string,
  sessionId: string,
  syncToken?: string,
): string {
  const url = `${apiBase.replace(/\/$/, "")}/api/sessions/${encodeURIComponent(sessionId)}/stream`
  return syncToken ? `${url}?sync_token=${encodeURIComponent(syncToken)}` : url
}
