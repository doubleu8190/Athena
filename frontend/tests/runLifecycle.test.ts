import assert from "node:assert/strict"
import test from "node:test"
import { canApplyRunStatusEvent, canSubmitNewRun, isSessionRunTerminal, preserveTerminalRunStatuses, shouldApplyRunStatusSnapshot, summarizeSessionRuns } from "../src/utils/runLifecycle.ts"

test("a terminal run releases the composer when the session has no queued work", () => {
  assert.deepEqual(summarizeSessionRuns({ first: "completed" }), {
    activeRunId: null,
    hasQueuedRuns: false,
    agentStatus: "idle",
  })
  assert.equal(isSessionRunTerminal("completed"), true)
  assert.equal(isSessionRunTerminal("failed"), true)
  assert.equal(isSessionRunTerminal("cancelled"), true)
})

test("a terminal run does not release the composer while another run is queued", () => {
  assert.deepEqual(summarizeSessionRuns({ first: "completed", second: "queued" }), {
    activeRunId: null,
    hasQueuedRuns: true,
    agentStatus: "queued",
  })
})

test("an active run remains authoritative while other runs are queued", () => {
  assert.deepEqual(summarizeSessionRuns({ first: "queued", second: "running" }), {
    activeRunId: "second",
    hasQueuedRuns: true,
    agentStatus: "running",
  })
  assert.deepEqual(summarizeSessionRuns({ first: "stopping", second: "queued" }), {
    activeRunId: "first",
    hasQueuedRuns: true,
    agentStatus: "cancelling",
  })
})

test("unknown and suspended states are not terminal lifecycle values", () => {
  assert.equal(isSessionRunTerminal("root_suspended"), false)
  assert.equal(isSessionRunTerminal("queued"), false)
})

test("the composer permits one queued run while stopping but blocks queued or running work", () => {
  assert.equal(canSubmitNewRun({ run: "stopping" }), true)
  assert.equal(canSubmitNewRun({ run: "stopping", next: "queued" }), false)
  assert.equal(canSubmitNewRun({ run: "running" }), false)
  assert.equal(canSubmitNewRun({ run: "queued" }), false)
  assert.equal(canSubmitNewRun({ run: "cancelled" }), true)
  assert.equal(canSubmitNewRun({ run: "completed", next: "queued" }), false)
})

test("lifecycle snapshots cannot overwrite newer SSE events or another session", () => {
  const base = {
    disposed: false,
    snapshotSessionId: "session-a",
    activeSessionId: "session-a",
    snapshotRevision: 4,
    currentRevision: 4,
    snapshotStoreRevision: 7,
    currentStoreRevision: 7,
    requestGeneration: 2,
    currentGeneration: 2,
  }
  assert.equal(shouldApplyRunStatusSnapshot(base), true)
  assert.equal(shouldApplyRunStatusSnapshot({ ...base, currentRevision: 5 }), false)
  assert.equal(shouldApplyRunStatusSnapshot({ ...base, currentStoreRevision: 8 }), false)
  assert.equal(shouldApplyRunStatusSnapshot({ ...base, activeSessionId: "session-b" }), false)
  assert.equal(shouldApplyRunStatusSnapshot({ ...base, currentGeneration: 3 }), false)
})

test("replayed non-terminal events cannot regress a terminal Run", () => {
  assert.equal(canApplyRunStatusEvent("completed", "queued"), false)
  assert.equal(canApplyRunStatusEvent("failed", "running"), false)
  assert.equal(canApplyRunStatusEvent("cancelled", "stopping"), false)
  assert.equal(canApplyRunStatusEvent("completed", "completed"), true)
  assert.deepEqual(
    preserveTerminalRunStatuses({ finished: "completed" }, { finished: "queued", next: "running" }),
    { finished: "completed", next: "running" },
  )
})
