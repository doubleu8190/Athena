import assert from "node:assert/strict"
import test from "node:test"
import { getActivityDuration, getRunTiming } from "../src/components/pages/runTiming.ts"
import type { RunTimelineEvent, SessionRunSummary } from "../src/types/index.ts"

const run: SessionRunSummary = {
  run_id: "run-1", session_id: "session-1", root_thread_id: "thread-1", status: "completed",
  created_at: "2026-10-23T05:58:14.318946Z",
  started_at: "2026-10-23T06:05:24.363067Z",
  updated_at: "2026-10-23T06:05:28.961866Z",
}

test("historical run separates the screenshot's queue and execution times", () => {
  const timing = getRunTiming(run)
  assert.ok(Math.abs(timing.executionMs! - 4599) < 2)
  assert.ok(Math.abs(timing.queueMs! - 430044) < 2)
  assert.equal(timing.queueMs! + timing.executionMs!, timing.totalMs)
})

test("resume and later event pages preserve the first execution start", () => {
  const resumed: RunTimelineEvent = {
    run_id: run.run_id, session_seq: 300, event_type: "run.resumed",
    occurred_at: "2026-10-23T06:05:27Z", payload: {},
  }
  assert.equal(getRunTiming(run, [resumed]).startedAt, Date.parse(run.started_at!))
})

test("running durations use the clock and ignore last projection update time", () => {
  const now = Date.parse("2026-10-23T06:05:30Z")
  const timing = getRunTiming({ ...run, status: "running" }, [], now)
  assert.equal(timing.executionMs, now - Date.parse(run.started_at!))
  assert.equal(timing.queueMs! + timing.executionMs!, timing.totalMs)
})

test("cancelled before execution has only queue time and never invents execution", () => {
  const timing = getRunTiming({ ...run, started_at: null, status: "cancelled" })
  assert.equal(timing.executionMs, null)
  assert.equal(timing.queueMs, timing.totalMs)
})

test("terminal run without a recorded start reports unknown execution", () => {
  assert.equal(getRunTiming({ ...run, started_at: null }).executionMs, null)
})

test("outbox terminal events without duration infer the activity interval", () => {
  assert.equal(getActivityDuration(run.started_at, run.updated_at, undefined), "4.6 s")
  assert.equal(getActivityDuration(run.started_at, run.updated_at, 4489), "4.5 s")
  assert.equal(getActivityDuration(undefined, run.updated_at, undefined), "—")
})
