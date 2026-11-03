export type SessionRunStatus = "queued" | "running" | "stopping" | "completed" | "failed" | "cancelled"

const activeStatuses = new Set<string>(["running", "stopping"])

export function summarizeSessionRuns(statuses: Record<string, string>): {
  activeRunId: string | null
  hasQueuedRuns: boolean
  agentStatus: "idle" | "queued" | "running" | "cancelling"
} {
  const active = Object.entries(statuses).find(([, status]) => activeStatuses.has(status))
  const hasQueuedRuns = Object.values(statuses).some((status) => status === "queued")
  return {
    activeRunId: active?.[0] ?? null,
    hasQueuedRuns,
    agentStatus: active?.[1] === "stopping" ? "cancelling" : active ? "running" : hasQueuedRuns ? "queued" : "idle",
  }
}

export function isSessionRunTerminal(status: string): boolean {
  return status === "completed" || status === "failed" || status === "cancelled"
}

export function canSubmitNewRun(statuses: Record<string, string>): boolean {
  return !Object.values(statuses).some((status) => status === "queued" || status === "running")
}

export function preserveTerminalRunStatuses(
  current: Record<string, string>,
  incoming: Record<string, string>,
): Record<string, string> {
  const merged = { ...incoming }
  for (const [runId, status] of Object.entries(current)) {
    if (isSessionRunTerminal(status) && !canApplyRunStatusEvent(status, merged[runId] || "")) merged[runId] = status
  }
  return merged
}

export function canApplyRunStatusEvent(currentStatus: string | undefined, incomingStatus: string): boolean {
  return !currentStatus || !isSessionRunTerminal(currentStatus) || incomingStatus === currentStatus
}

export function runStatusFromEvent(eventType: string): SessionRunStatus | null {
  switch (eventType) {
    case "run.queued": return "queued"
    case "run.started":
    case "run.resumed": return "running"
    case "run.stopping": return "stopping"
    case "run.completed": return "completed"
    case "run.failed":
    case "run.budget_exceeded": return "failed"
    case "run.cancelled": return "cancelled"
    default: return null
  }
}

export function shouldApplyRunStatusSnapshot(input: {
  disposed: boolean
  snapshotSessionId: string
  activeSessionId: string | null
  snapshotRevision: number
  currentRevision: number
  snapshotStoreRevision: number
  currentStoreRevision: number
  requestGeneration: number
  currentGeneration: number
}): boolean {
  return !input.disposed
    && input.snapshotSessionId === input.activeSessionId
    && input.snapshotRevision === input.currentRevision
    && input.snapshotStoreRevision === input.currentStoreRevision
    && input.requestGeneration === input.currentGeneration
}
