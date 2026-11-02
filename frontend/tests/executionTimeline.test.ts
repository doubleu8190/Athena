import assert from "node:assert/strict"
import test from "node:test"
import { closeTimelineEntry, upsertTimelineEntry, workerResultText } from "../src/utils/executionTimeline.ts"

test("terminal events close only an existing started row and preserve its order", () => {
  const started = {
    id: "task:plan:task:execution:task.started",
    session_seq: 12,
    run_id: "run-1",
    event_type: "task.started",
    label: "已分配给Worker A，准备执行：写作",
    status: "running" as const,
    timestamp: "2026-11-01T00:00:01Z",
  }
  const inserted = upsertTimelineEntry([], {}, started)
  const { entries } = closeTimelineEntry(inserted, {}, { ...started, status: "completed" })
  const closed = entries[0]
  assert.equal(closed.status, "completed")
  assert.equal(closed.timestamp, started.timestamp)
  assert.equal(closed.session_seq, started.session_seq)
})

test("terminal before started does not reopen a spinner or fabricate a row", () => {
  const started = {
    id: "task:plan:task:execution:task.started",
    event_type: "task.started",
    label: "已分配给Worker A，准备执行：写作",
    status: "running" as const,
    timestamp: "2026-11-01T00:00:01Z",
  }
  const closed = closeTimelineEntry([], {}, { ...started, status: "cancelled" })
  assert.equal(closed.entries.length, 0)
  const lateStarted = upsertTimelineEntry(closed.entries, closed.terminalMarkers, started)
  assert.equal(lateStarted[0]?.status, "cancelled")
  assert.deepEqual(lateStarted.map((entry) => entry.id), [started.id])
})

test("late started events are ordered before already received terminal events", () => {
  const terminal = {
    id: "task:plan:task:execution:task.cancelled",
    session_seq: 14,
    run_id: "run-1",
    event_type: "task.cancelled",
    label: "已取消",
    status: "cancelled" as const,
    timestamp: "2026-11-01T00:00:02Z",
  }
  const started = {
    id: "task:plan:task:execution:task.started",
    session_seq: 13,
    run_id: "run-1",
    event_type: "task.started",
    label: "已开始",
    status: "running" as const,
    timestamp: "2026-11-01T00:00:01Z",
  }
  const terminalFirst = upsertTimelineEntry([], {}, terminal)
  const ordered = upsertTimelineEntry(terminalFirst, {}, started)
  assert.deepEqual(ordered.map((entry) => entry.event_type), ["task.started", "task.cancelled"])
})

test("a late cancellation request cannot downgrade confirmed cancellation", () => {
  const entry = {
    id: "task:plan:task:execution:task.started",
    event_type: "task.started",
    label: "已开始",
    status: "running" as const,
    timestamp: "2026-11-01T00:00:01Z",
  }
  const cancelled = closeTimelineEntry([], {}, { ...entry, status: "cancelled" })
  const lateCancelling = closeTimelineEntry(
    cancelled.entries,
    cancelled.terminalMarkers,
    { ...entry, status: "waiting" },
  )
  assert.equal(lateCancelling.entries.length, 0)
  assert.equal(lateCancelling.terminalMarkers[entry.id], "cancelled")
})

test("stop confirmation closes the cancelling row and keeps its original order", () => {
  const cancelling = {
    id: "task:plan:task:execution:task.cancelling",
    session_seq: 21,
    run_id: "run-1",
    event_type: "task.cancelling",
    label: "正在请求 Worker 停止",
    status: "waiting" as const,
    timestamp: "2026-11-01T00:00:01Z",
  }
  const startedRows = upsertTimelineEntry([], {}, cancelling)
  const confirmed = closeTimelineEntry(startedRows, {}, { ...cancelling, status: "cancelled" })
  assert.equal(confirmed.entries[0]?.status, "cancelled")
  assert.equal(confirmed.entries[0]?.timestamp, cancelling.timestamp)
  assert.equal(confirmed.entries[0]?.session_seq, cancelling.session_seq)
})

test("confirmation received before task.cancelling keeps the late row terminal", () => {
  const cancelling = {
    id: "task:plan:task:execution:task.cancelling",
    session_seq: 22,
    event_type: "task.cancelling",
    label: "正在请求 Worker 停止",
    status: "waiting" as const,
    timestamp: "2026-11-01T00:00:02Z",
  }
  const confirmed = closeTimelineEntry([], {}, { ...cancelling, status: "cancelled" })
  const lateEvent = upsertTimelineEntry(confirmed.entries, confirmed.terminalMarkers, cancelling)
  assert.equal(lateEvent[0]?.status, "cancelled")
  assert.equal(lateEvent[0]?.timestamp, cancelling.timestamp)
  assert.equal(lateEvent[0]?.session_seq, cancelling.session_seq)
})

test("single text result is displayed as decoded text with its newlines", () => {
  const content = "第一段\n第二段"
  assert.equal(workerResultText({ content }, "", false), content)
  assert.equal(workerResultText({ output: content }, "", false), content)
})

test("multi-field result stays complete and readable as formatted JSON", () => {
  const result = { summary: "通过", content: "完整审核意见\n包含第二行", score: 0.98 }
  const displayed = workerResultText(result, "", false)
  assert.ok(displayed)
  assert.match(displayed, /\n  "summary"/)
  assert.deepEqual(JSON.parse(displayed), result)
})
