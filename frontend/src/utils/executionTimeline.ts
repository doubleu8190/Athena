import type { ExecutionTimelineEntry } from "../types/index.ts"

export type TimelineTerminalMarkers = Record<string, ExecutionTimelineEntry["status"]>

export function workerResultText(
  value: unknown,
  resultRef: string,
  availableInFinal: boolean,
): string | null {
  const record = value && typeof value === "object"
    ? value as Record<string, unknown>
    : undefined
  const reference = resultRef || (typeof record?.result_ref === "string" ? record.result_ref : "")
  if (reference || availableInFinal) return "结果已保存，完整内容由最终答复汇总。"
  let candidate: string
  if (typeof value === "string") {
    candidate = value
  } else if (record) {
    const textFields = ["output", "content", "summary", "result", "message"]
    const matchingFields = textFields.filter((key) => typeof record[key] === "string")
    candidate = Object.keys(record).length === 1 && matchingFields.length === 1
      ? record[matchingFields[0]] as string
      : JSON.stringify(record, null, 2)
  } else {
    candidate = ""
  }
  if (!candidate.trim()) return null
  return candidate.length > 12000
    ? `${candidate.slice(0, 12000)}…（结果过长，已截断）`
    : candidate
}

export function upsertTimelineEntry(
  entries: ExecutionTimelineEntry[],
  terminalMarkers: TimelineTerminalMarkers,
  entry: ExecutionTimelineEntry,
): ExecutionTimelineEntry[] {
  const resolvedEntry = ["task.started", "task.cancelling"].includes(entry.event_type)
    && terminalMarkers[entry.id]
    ? { ...entry, status: terminalMarkers[entry.id] }
    : entry
  const existingIndex = entries.findIndex((item) => item.id === entry.id)
  const next = [...entries]
  if (existingIndex < 0) {
    next.push(resolvedEntry)
  } else {
    next[existingIndex] = {
      ...entries[existingIndex],
      ...resolvedEntry,
      metadata: { ...entries[existingIndex].metadata, ...resolvedEntry.metadata },
    }
  }
  return next.sort((left, right) => {
    if (left.session_seq !== undefined && right.session_seq !== undefined) {
      return left.session_seq - right.session_seq
    }
    return left.timestamp.localeCompare(right.timestamp)
  })
}

export function closeTimelineEntry(
  entries: ExecutionTimelineEntry[],
  terminalMarkers: TimelineTerminalMarkers,
  entry: ExecutionTimelineEntry,
): { entries: ExecutionTimelineEntry[]; terminalMarkers: TimelineTerminalMarkers } {
  const previousStatus = terminalMarkers[entry.id]
  const previousIsTerminal = ["completed", "failed", "cancelled"].includes(previousStatus || "")
  const nextStatus = previousIsTerminal && !["completed", "failed", "cancelled"].includes(entry.status)
    ? previousStatus
    : entry.status
  const nextMarkers = { ...terminalMarkers, [entry.id]: nextStatus }
  const index = entries.findIndex((item) => item.id === entry.id)
  if (index < 0) return { entries, terminalMarkers: nextMarkers }
  const nextEntries = [...entries]
  nextEntries[index] = { ...entries[index], status: nextStatus }
  return { entries: nextEntries, terminalMarkers: nextMarkers }
}
