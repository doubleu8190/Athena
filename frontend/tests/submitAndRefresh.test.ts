import assert from "node:assert/strict"
import test from "node:test"
import { apiClient } from "../src/api/client.ts"
import type { ApprovalRequest } from "../src/types/index.ts"
import type { ApprovalStatusSnapshot } from "../src/types/index.ts"
import { submitAndRefresh } from "../src/utils/submitAndRefresh.ts"
import {
  clearApprovalSubmissionUncertainty,
  getConflictingUncertainApprovalIds,
  getUncertainApprovalDecisions,
  getUncertainApprovalIds,
  getSubmittableApprovalRequests,
  isApprovalBatchComplete,
  hasApprovalDecisionToSubmit,
  hasSubmittableApprovalRequests,
  markApprovalSubmissionsUncertain,
  reconcileApprovalSubmission,
  retainApprovalIds,
  splitApprovalRefreshTargets,
  syncApprovalDecisionStorage,
} from "../src/utils/approvalSubmission.ts"

function statuses(...values: ApprovalStatusSnapshot[]): Map<string, ApprovalStatusSnapshot> {
  return new Map(values.map((value) => [value.approval_id, value]))
}

test("refresh failure does not reject a successfully submitted action", async () => {
  let submissions = 0
  const { submission, refreshed } = await submitAndRefresh(
    async () => { submissions += 1 },
    async () => { throw new Error("snapshot unavailable") },
  )

  assert.equal(submissions, 1)
  assert.equal(submission, undefined)
  assert.equal(refreshed, false)
})

test("submission failure rejects and does not refresh", async () => {
  let refreshes = 0
  await assert.rejects(
    submitAndRefresh(
      async () => { throw new Error("submission unavailable") },
      async () => { refreshes += 1 },
    ),
    /submission unavailable/,
  )
  assert.equal(refreshes, 0)
})

test("approval batch reports individual successes so retries can exclude them", async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (input) => {
    const url = String(input)
    return url.includes("approval-done")
      ? new Response(null, { status: 204 })
      : new Response(JSON.stringify({ detail: "already resolved" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        })
  }
  try {
    const requests = ["approval-done", "approval-retry"].map((approval_id) => ({ approval_id }) as ApprovalRequest)
    const result = await apiClient.respondApprovalsIndividually(requests, {
      "approval-done": "approved",
      "approval-retry": "denied",
    })
    assert.deepEqual(result.succeededApprovalIds, ["approval-done"])
    assert.deepEqual(result.failures.map((failure) => failure.approvalId), ["approval-retry"])
    assert.deepEqual(result.statusUnknownApprovalIds, [])
    assert.deepEqual(result.retryableApprovalIds, [])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("a cross-tab decision conflict is not reported as a successful submission", async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "approval_not_pending" }), {
    status: 409,
    headers: { "Content-Type": "application/json" },
  })
  try {
    const approval = { approval_id: "approval-cross-tab", session_id: "session-1" } as ApprovalRequest
    markApprovalSubmissionsUncertain({ "approval-cross-tab": "denied" })
    const result = await apiClient.respondApprovalsIndividually([approval], {
      "approval-cross-tab": "denied",
    })
    assert.deepEqual(result.succeededApprovalIds, [])
    assert.deepEqual(result.failures.map((failure) => failure.approvalId), ["approval-cross-tab"])
    assert.deepEqual(result.statusUnknownApprovalIds, [])

    const reconciled = reconcileApprovalSubmission(
      result,
      statuses({ approval_id: "approval-cross-tab", resolved: true, resolution: "approved" }),
    )
    assert.deepEqual(reconciled.retryableApprovalIds, [])
    assert.deepEqual(reconciled.succeededApprovalIds, [])
    assert.match(reconciled.failures[0].message, /already resolved|approval_not_pending/)
  } finally {
    globalThis.fetch = originalFetch
    clearApprovalSubmissionUncertainty(["approval-cross-tab"])
  }
})

test("reconciliation compares authority with this request decision, not shared tab state", () => {
  const approvalId = "approval-lost-response"
  const attemptId = markApprovalSubmissionsUncertain({ [approvalId]: "denied" })
  const result = {
    attemptId,
    submittedDecisions: { [approvalId]: "approved" },
    succeededApprovalIds: [],
    failures: [{ approvalId, message: "response lost" }],
    retryableApprovalIds: [],
    recoverableApprovalIds: [],
    statusUnknownApprovalIds: [approvalId],
  }
  const reconciled = reconcileApprovalSubmission(
    result,
    statuses({ approval_id: approvalId, resolved: true, resolution: "denied" }),
  )
  assert.deepEqual(reconciled.succeededApprovalIds, [])
  assert.deepEqual(reconciled.retryableApprovalIds, [])
  assert.equal(reconciled.failures.length, 1)
  const winningTab = reconcileApprovalSubmission(
    { ...result, submittedDecisions: { [approvalId]: "denied" } },
    statuses({ approval_id: approvalId, resolved: true, resolution: "denied" }),
  )
  assert.deepEqual(winningTab.succeededApprovalIds, [approvalId])
  clearApprovalSubmissionUncertainty([approvalId])
})

test("network failure leaves the approval outcome unknown until authoritative confirmation", async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => { throw new TypeError("connection reset") }
  const attemptId = markApprovalSubmissionsUncertain({ "approval-network-unknown": "approved" })
  try {
    const result = await apiClient.respondApprovalsIndividually(
      [{ approval_id: "approval-network-unknown" } as ApprovalRequest],
      { "approval-network-unknown": "approved" },
      attemptId,
    )
    assert.deepEqual(result.statusUnknownApprovalIds, ["approval-network-unknown"])
    const refreshed = reconcileApprovalSubmission(result, statuses({ approval_id: "approval-network-unknown", resolved: false, resolution: "pending" }))
    assert.deepEqual(refreshed.retryableApprovalIds, [])
    assert.deepEqual(refreshed.statusUnknownApprovalIds, ["approval-network-unknown"])
  } finally {
    globalThis.fetch = originalFetch
    clearApprovalSubmissionUncertainty(["approval-network-unknown"])
  }
})

test("failed approvals become retryable only after refresh confirms they remain pending", () => {
  const result = {
    attemptId: markApprovalSubmissionsUncertain({ "still-pending": "approved" }),
    submittedDecisions: { "still-pending": "approved", "resolved-elsewhere": "denied" },
    succeededApprovalIds: [],
    failures: [
      { approvalId: "still-pending", message: "network error" },
      { approvalId: "resolved-elsewhere", message: "conflict" },
    ],
    retryableApprovalIds: [],
    recoverableApprovalIds: [],
    statusUnknownApprovalIds: ["still-pending"],
  }
  const pendingStatus = statuses(
    { approval_id: "still-pending", resolved: false, resolution: "pending" },
    { approval_id: "resolved-elsewhere", resolved: true, resolution: "denied" },
  )
  const refreshed = reconcileApprovalSubmission(result, pendingStatus)
  assert.deepEqual(refreshed.retryableApprovalIds, [])
  assert.deepEqual(refreshed.recoverableApprovalIds, ["still-pending"])
  assert.deepEqual(refreshed.statusUnknownApprovalIds, ["still-pending"])

  const unavailable = reconcileApprovalSubmission(result, null, "snapshot refresh failed")
  assert.deepEqual(unavailable.retryableApprovalIds, [])
  assert.deepEqual(unavailable.recoverableApprovalIds, [])
  assert.deepEqual(unavailable.statusUnknownApprovalIds, ["still-pending", "resolved-elsewhere"])
  assert.equal(unavailable.refreshError, "snapshot refresh failed")

  const knownRejection = reconcileApprovalSubmission({
    ...result,
    statusUnknownApprovalIds: [],
  }, pendingStatus)
  assert.deepEqual(knownRejection.retryableApprovalIds, ["still-pending"])
  clearApprovalSubmissionUncertainty(["still-pending"])
})

test("a batch with no confirmed pending items cannot submit an empty decision", () => {
  const requests = ["unknown", "already-submitted"].map((approval_id) => ({ approval_id }) as ApprovalRequest)
  assert.equal(hasSubmittableApprovalRequests(requests, new Set(["already-submitted"]), new Set(["unknown"])), false)
  assert.equal(hasSubmittableApprovalRequests(requests, new Set(), new Set(["unknown"])), true)
})

test("unknown Worker and regular approvals use their authoritative refresh sources", () => {
  const requests = [
    { approval_id: "worker", task_id: "task-1", run_id: "run-1" },
    { approval_id: "regular", run_id: "run-1" },
  ].map((request) => request as ApprovalRequest)
  const targets = splitApprovalRefreshTargets(requests)
  assert.deepEqual(targets.workerRequests.map((request) => request.approval_id), ["worker"])
  assert.deepEqual(targets.sessionRequests.map((request) => request.approval_id), ["regular"])
})

test("SSE removal of a successful approval retains locks for still-visible unknown approvals", () => {
  const submitted = retainApprovalIds(new Set(["succeeded"]), new Set(["uncertain"]))
  const blocked = retainApprovalIds(new Set(["uncertain", "removed"]), new Set(["uncertain"]))
  const visible = [{ approval_id: "uncertain" } as ApprovalRequest]
  assert.deepEqual([...submitted], [])
  assert.deepEqual([...blocked], ["uncertain"])
  assert.equal(hasSubmittableApprovalRequests(visible, submitted, blocked), false)
})

test("uncertain approval decisions use independent storage keys and merge cross-tab updates", () => {
  const previousStorage = globalThis.localStorage
  const values = new Map<string, string>()
  globalThis.localStorage = {
    get length() { return values.size },
    key(index: number) { return [...values.keys()][index] ?? null },
    getItem(key: string) { return values.get(key) ?? null },
    setItem(key: string, value: string) { values.set(key, value) },
    removeItem(key: string) { values.delete(key) },
    clear() { values.clear() },
  }
  try {
    markApprovalSubmissionsUncertain({ first: "approved", second: "denied" })
    const approvalAttemptA = markApprovalSubmissionsUncertain({ cloned: "approved" })
    const approvalAttemptB = markApprovalSubmissionsUncertain({ cloned: "denied" })
    assert.notEqual(approvalAttemptA, approvalAttemptB)
    assert.equal(getUncertainApprovalDecisions().has("cloned"), false)
    assert.equal(getUncertainApprovalIds().has("cloned"), true)
    clearApprovalSubmissionUncertainty(["cloned"], approvalAttemptA)
    assert.equal(getUncertainApprovalIds().has("cloned"), true)
    assert.equal(getUncertainApprovalDecisions().get("cloned"), "denied")
    const storedDecisions = [...values.entries()].filter(([key]) => key.startsWith("athena:uncertain-approval-submission:"))
    assert.equal(storedDecisions.length, 3)
    const remoteKey = "athena:uncertain-approval-submission:third:remote-attempt"
    values.set(remoteKey, "approved")
    syncApprovalDecisionStorage(remoteKey, "approved")
    assert.equal(getUncertainApprovalDecisions().get("third"), "approved")
    values.delete(remoteKey)
    syncApprovalDecisionStorage(remoteKey, null)
  } finally {
    clearApprovalSubmissionUncertainty(["first", "second", "third"])
    globalThis.localStorage = previousStorage
  }
})

test("a pending snapshot does not unlock an ambiguous request, while terminal confirmation clears it", () => {
  const attemptId = markApprovalSubmissionsUncertain({ "uncertain-id": "approved" })
  const result = {
    attemptId,
    submittedDecisions: { "uncertain-id": "approved" },
    succeededApprovalIds: [],
    failures: [{ approvalId: "uncertain-id", message: "network response lost" }],
    retryableApprovalIds: [],
    recoverableApprovalIds: [],
    statusUnknownApprovalIds: ["uncertain-id"],
  }
  const stillPending = reconcileApprovalSubmission(result, statuses({ approval_id: "uncertain-id", resolved: false, resolution: "pending" }))
  assert.deepEqual(stillPending.retryableApprovalIds, [])
  assert.deepEqual(stillPending.recoverableApprovalIds, ["uncertain-id"])
  assert.deepEqual(stillPending.statusUnknownApprovalIds, ["uncertain-id"])
  assert.equal(getUncertainApprovalDecisions().get("uncertain-id"), "approved")
  const visible = [{ approval_id: "uncertain-id" } as ApprovalRequest]
  assert.equal(hasApprovalDecisionToSubmit(visible, new Set(), new Set(["uncertain-id"]), new Set(["uncertain-id"]), new Set(), { "uncertain-id": "approved" }), true)
  assert.equal(hasApprovalDecisionToSubmit(visible, new Set(), new Set(["uncertain-id"]), new Set(["uncertain-id"]), new Set(), { "uncertain-id": "denied" }), false)

  const noLongerPending = reconcileApprovalSubmission(result, statuses({ approval_id: "uncertain-id", resolved: true, resolution: "denied" }))
  assert.deepEqual(noLongerPending.statusUnknownApprovalIds, [])
  assert.equal(getUncertainApprovalDecisions().has("uncertain-id"), false)
  clearApprovalSubmissionUncertainty(["uncertain-id"])
})

test("conflicting unknown attempts allow a fresh explicit arbitration decision", () => {
  const approvalId = "conflicting-unknown"
  const firstAttempt = markApprovalSubmissionsUncertain({ [approvalId]: "approved" })
  const secondAttempt = markApprovalSubmissionsUncertain({ [approvalId]: "denied" })
  const requests = [{ approval_id: approvalId } as ApprovalRequest]
  const arbitrable = getConflictingUncertainApprovalIds()

  assert.notEqual(firstAttempt, secondAttempt)
  assert.equal(arbitrable.has(approvalId), true)
  assert.equal(hasApprovalDecisionToSubmit(
    requests,
    new Set(),
    new Set([approvalId]),
    new Set(),
    arbitrable,
    { [approvalId]: "approved" },
  ), true)
  const arbitrationAttempt = markApprovalSubmissionsUncertain({ [approvalId]: "approved" })
  const arbitrationResult = reconcileApprovalSubmission({
    attemptId: arbitrationAttempt,
    submittedDecisions: { [approvalId]: "approved" },
    succeededApprovalIds: [],
    failures: [{ approvalId, message: "decision arbitration request timed out" }],
    retryableApprovalIds: [],
    recoverableApprovalIds: [],
    statusUnknownApprovalIds: [approvalId],
  }, statuses({ approval_id: approvalId, resolved: false, resolution: "pending" }))
  assert.deepEqual(arbitrationResult.recoverableApprovalIds, [approvalId])
  assert.deepEqual(arbitrationResult.submittedDecisions, { [approvalId]: "approved" })
  const retryPayload = getSubmittableApprovalRequests(
    requests,
    new Set(),
    new Set([approvalId]),
    new Set(arbitrationResult.recoverableApprovalIds),
    arbitrable,
    { [approvalId]: "approved" },
  )
  assert.deepEqual(retryPayload.map((request) => request.approval_id), [approvalId])
  assert.deepEqual(Object.fromEntries(retryPayload.map((request) => [request.approval_id, "approved"])), {
    [approvalId]: "approved",
  })
  assert.equal(hasApprovalDecisionToSubmit(
    requests,
    new Set(),
    new Set([approvalId]),
    new Set(),
    arbitrable,
    { [approvalId]: "denied" },
  ), true)
  clearApprovalSubmissionUncertainty([approvalId])
})

test("mixed success and unknown approvals remain open until every item is confirmed", () => {
  const requests = ["approval-a", "approval-b"].map((approval_id) => ({ approval_id }) as ApprovalRequest)
  const confirmed = new Set(["approval-b"])
  assert.equal(isApprovalBatchComplete(requests, confirmed), false)
  confirmed.add("approval-a")
  assert.equal(isApprovalBatchComplete(requests, confirmed), true)
})
