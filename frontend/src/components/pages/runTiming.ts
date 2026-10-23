import type { RunTimelineEvent, SessionRunSummary } from "../../types"

export function formatDuration(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—"
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`
}

function timestamp(value: unknown): number | null {
  if (typeof value !== "string") return null
  const result = Date.parse(value)
  return Number.isFinite(result) ? result : null
}

export function getRunTiming(run: SessionRunSummary, events: RunTimelineEvent[] = [], now = Date.now()) {
  const ownEvents = events.filter((event) => event.run_id === run.run_id)
  const starts = [timestamp(run.started_at), ...ownEvents
    .filter((event) => event.event_type === "run.started" || event.event_type === "run.resumed")
    .map((event) => timestamp(event.payload.started_at) ?? timestamp(event.occurred_at))]
    .filter((value): value is number => value != null)
  const startedAt = starts.length ? Math.min(...starts) : null
  const terminal = ["completed", "failed", "cancelled"].includes(run.status)
  const endEvent = ownEvents.find((event) => ["run.completed", "run.failed", "run.cancelled"].includes(event.event_type))
  const endedAt = endEvent
    ? timestamp(endEvent.payload.completed_at) ?? timestamp(endEvent.occurred_at)
    : terminal ? timestamp(run.updated_at) : null
  const createdAt = timestamp(run.created_at)
  const end = endedAt ?? (terminal ? null : now)
  const queueEnd = startedAt ?? (!terminal || run.status === "cancelled" ? end : null)
  return {
    startedAt,
    endedAt,
    executionMs: startedAt != null && end != null ? Math.max(0, end - startedAt) : null,
    queueMs: createdAt != null && queueEnd != null ? Math.max(0, queueEnd - createdAt) : null,
    totalMs: createdAt != null && end != null ? Math.max(0, end - createdAt) : null,
  }
}

export function getActivityDuration(startedAt: unknown, completedAt: unknown, durationMs: unknown): string {
  if (typeof durationMs === "number" && Number.isFinite(durationMs)) return formatDuration(durationMs)
  const start = timestamp(startedAt)
  const end = timestamp(completedAt)
  return formatDuration(start != null && end != null ? Math.max(0, end - start) : null)
}
