import type { ApprovalSubmissionResult } from "../api/client"
import type { ApprovalRequest, ApprovalStatusSnapshot } from "../types"

const uncertainApprovalStoragePrefix = "athena:uncertain-approval-submission:"
type ApprovalAttemptMap = Map<string, Map<string, "approved" | "denied">>
const uncertainApprovalAttempts: ApprovalAttemptMap = new Map()

function createAttemptId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `attempt-${Date.now()}-${Math.random()}`
}

function approvalStorageKey(approvalId: string, attemptId: string): string {
  return `${uncertainApprovalStoragePrefix}${encodeURIComponent(approvalId)}:${encodeURIComponent(attemptId)}`
}

function parseApprovalStorageKey(key: string): { approvalId: string; attemptId: string } | null {
  if (!key.startsWith(uncertainApprovalStoragePrefix)) return null
  const separator = key.indexOf(":", uncertainApprovalStoragePrefix.length)
  if (separator < 0) return null
  try {
    return {
      approvalId: decodeURIComponent(key.slice(uncertainApprovalStoragePrefix.length, separator)),
      attemptId: decodeURIComponent(key.slice(separator + 1)),
    }
  } catch {
    return null
  }
}

function setApprovalAttempt(approvalId: string, attemptId: string, decision: "approved" | "denied"): void {
  const attempts = uncertainApprovalAttempts.get(approvalId) ?? new Map<string, "approved" | "denied">()
  attempts.set(attemptId, decision)
  uncertainApprovalAttempts.set(approvalId, attempts)
}

function deleteApprovalAttempt(approvalId: string, attemptId: string): void {
  const attempts = uncertainApprovalAttempts.get(approvalId)
  attempts?.delete(attemptId)
  if (!attempts?.size) uncertainApprovalAttempts.delete(approvalId)
}

function refreshApprovalAttempts(): void {
  try {
    const storage = globalThis.localStorage
    if (!storage) return
    uncertainApprovalAttempts.clear()
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index)
      if (!key) continue
      const parsedKey = parseApprovalStorageKey(key)
      const decision = storage.getItem(key)
      if (!parsedKey || (decision !== "approved" && decision !== "denied")) continue
      setApprovalAttempt(parsedKey.approvalId, parsedKey.attemptId, decision)
    }
  } catch {
    // Storage access may be disabled; in-memory state still protects this tab.
  }
}

refreshApprovalAttempts()

function persistApprovalAttempt(approvalId: string, attemptId: string, decision: "approved" | "denied"): void {
  try {
    globalThis.localStorage?.setItem(approvalStorageKey(approvalId, attemptId), decision)
  } catch {
    // The in-memory registry still protects approvals for the current page lifetime.
  }
}

/** 同步另一浏览器标签页写入或清除的单条审批决定。 */
export function syncApprovalDecisionStorage(key: string, value: string | null): void {
  const parsedKey = parseApprovalStorageKey(key)
  if (!parsedKey) return
  if (value === "approved" || value === "denied") {
    setApprovalAttempt(parsedKey.approvalId, parsedKey.attemptId, value)
  } else {
    deleteApprovalAttempt(parsedKey.approvalId, parsedKey.attemptId)
  }
}

export function approvalIdFromStorageKey(key: string): string | null {
  return parseApprovalStorageKey(key)?.approvalId ?? null
}

if (typeof globalThis.addEventListener === "function") {
  globalThis.addEventListener("storage", (event) => {
    const storageEvent = event as StorageEvent
    if (storageEvent.key) syncApprovalDecisionStorage(storageEvent.key, storageEvent.newValue)
  })
}

/** Keep in-flight or uncertain decisions locked across component and route refreshes. */
export function markApprovalSubmissionsUncertain(decisions: Record<string, "approved" | "denied">): string {
  const attemptId = createAttemptId()
  for (const [approvalId, decision] of Object.entries(decisions)) {
    setApprovalAttempt(approvalId, attemptId, decision)
    persistApprovalAttempt(approvalId, attemptId, decision)
  }
  return attemptId
}

/** Clear uncertainty only after a response or authoritative state confirms the outcome. */
export function clearApprovalSubmissionUncertainty(approvalIds: Iterable<string>, attemptId?: string): void {
  for (const approvalId of approvalIds) {
    try {
      const storage = globalThis.localStorage
      if (attemptId) {
        storage?.removeItem(approvalStorageKey(approvalId, attemptId))
        deleteApprovalAttempt(approvalId, attemptId)
      } else if (storage) {
        const keys = Array.from({ length: storage.length }, (_, index) => storage.key(index))
        for (const key of keys) {
          if (!key) continue
          const parsedKey = parseApprovalStorageKey(key)
          if (parsedKey?.approvalId === approvalId) {
            storage.removeItem(key)
            deleteApprovalAttempt(approvalId, parsedKey.attemptId)
          }
        }
      } else {
        uncertainApprovalAttempts.delete(approvalId)
      }
    } catch {
      // The in-memory registry still releases the approval for the current page.
    }
  }
}

export function getUncertainApprovalDecisions(): ReadonlyMap<string, "approved" | "denied"> {
  refreshApprovalAttempts()
  const decisions = new Map<string, "approved" | "denied">()
  for (const [approvalId, attempts] of uncertainApprovalAttempts) {
    const distinctDecisions = new Set(attempts.values())
    if (distinctDecisions.size === 1) decisions.set(approvalId, [...distinctDecisions][0])
  }
  return decisions
}

export function getUncertainApprovalAttempts(): ReadonlyMap<string, ReadonlyMap<string, "approved" | "denied">> {
  refreshApprovalAttempts()
  return new Map([...uncertainApprovalAttempts].map(([approvalId, attempts]) => [approvalId, new Map(attempts)]))
}

export function getUncertainApprovalIds(): ReadonlySet<string> {
  return new Set(getUncertainApprovalAttempts().keys())
}

/** Return approvals with conflicting unresolved decisions that need a fresh user arbitration. */
export function getConflictingUncertainApprovalIds(): ReadonlySet<string> {
  return new Set([...getUncertainApprovalAttempts()]
    .filter(([, attempts]) => new Set(attempts.values()).size > 1)
    .map(([approvalId]) => approvalId))
}

/** Retain state for approvals that remain visible after an SSE snapshot update. */
export function retainApprovalIds(
  trackedApprovalIds: ReadonlySet<string>,
  presentApprovalIds: ReadonlySet<string>,
): Set<string> {
  return new Set([...trackedApprovalIds].filter((approvalId) => presentApprovalIds.has(approvalId)))
}

/** Return whether a batch still contains an approval that can be submitted. */
export function hasSubmittableApprovalRequests(
  requests: ApprovalRequest[],
  submittedApprovalIds: ReadonlySet<string>,
  blockedApprovalIds: ReadonlySet<string>,
): boolean {
  return requests.some((request) =>
    !submittedApprovalIds.has(request.approval_id)
    && !blockedApprovalIds.has(request.approval_id),
  )
}

/** A batch is complete only when every visible approval has a confirmed decision. */
export function isApprovalBatchComplete(
  requests: ApprovalRequest[],
  confirmedApprovalIds: ReadonlySet<string>,
): boolean {
  return requests.length > 0 && requests.every((request) => confirmedApprovalIds.has(request.approval_id))
}

/** Allow an ambiguous request to be retried only with its original decision. */
export function hasApprovalDecisionToSubmit(
  requests: ApprovalRequest[],
  submittedApprovalIds: ReadonlySet<string>,
  blockedApprovalIds: ReadonlySet<string>,
  recoverableApprovalIds: ReadonlySet<string>,
  arbitrableApprovalIds: ReadonlySet<string>,
  decisions: Record<string, "approved" | "denied" | "cancelled">,
): boolean {
  return getSubmittableApprovalRequests(
    requests,
    submittedApprovalIds,
    blockedApprovalIds,
    recoverableApprovalIds,
    arbitrableApprovalIds,
    decisions,
  ).length > 0
}

/** Select the exact requests a submit action is allowed to send. */
export function getSubmittableApprovalRequests(
  requests: ApprovalRequest[],
  submittedApprovalIds: ReadonlySet<string>,
  blockedApprovalIds: ReadonlySet<string>,
  recoverableApprovalIds: ReadonlySet<string>,
  arbitrableApprovalIds: ReadonlySet<string>,
  decisions: Record<string, "approved" | "denied" | "cancelled">,
): ApprovalRequest[] {
  const uncertainDecisions = getUncertainApprovalDecisions()
  return requests.filter((request) => {
    const approvalId = request.approval_id
    const isArbitrating = arbitrableApprovalIds.has(approvalId)
    const isRecovering = recoverableApprovalIds.has(approvalId)
    return !submittedApprovalIds.has(approvalId)
      && (!blockedApprovalIds.has(approvalId) || isRecovering || isArbitrating)
      && (isArbitrating
        || !isRecovering
        || uncertainDecisions.get(approvalId) === decisions[approvalId])
      && Boolean(decisions[approvalId])
  })
}

/** Separate Worker approvals, which live in run snapshots, from session approvals. */
export function splitApprovalRefreshTargets(requests: ApprovalRequest[]): {
  workerRequests: ApprovalRequest[]
  sessionRequests: ApprovalRequest[]
} {
  const workerRequests: ApprovalRequest[] = []
  const sessionRequests: ApprovalRequest[] = []
  for (const request of requests) {
    if (request.task_id || request.plan_id || request.worker_run_id) {
      workerRequests.push(request)
    } else {
      sessionRequests.push(request)
    }
  }
  return { workerRequests, sessionRequests }
}

/** Retry only failed approvals that an authoritative refresh still reports as pending. */
export function reconcileApprovalSubmission(
  result: ApprovalSubmissionResult,
  approvalStatuses: ReadonlyMap<string, ApprovalStatusSnapshot> | null,
  refreshError?: string,
): ApprovalSubmissionResult {
  const ambiguousIds = new Set(result.statusUnknownApprovalIds)
  const retryableApprovalIds: string[] = []
  const recoverableApprovalIds: string[] = []
  const statusUnknownApprovalIds: string[] = []
  const succeededApprovalIds = [...result.succeededApprovalIds]
  const failures = result.failures.filter((failure) => {
    const approvalStatus = approvalStatuses?.get(failure.approvalId)
    if (!approvalStatus) {
      statusUnknownApprovalIds.push(failure.approvalId)
      return true
    }
    if (approvalStatus.resolved) {
      const requestedDecision = result.submittedDecisions[failure.approvalId]
        ?? getUncertainApprovalDecisions().get(failure.approvalId)
      clearApprovalSubmissionUncertainty([failure.approvalId])
      if (requestedDecision && approvalStatus.resolution === requestedDecision) {
        succeededApprovalIds.push(failure.approvalId)
        return false
      }
      return true
    }
    if (ambiguousIds.has(failure.approvalId)) {
      if (result.submittedDecisions[failure.approvalId]) {
        recoverableApprovalIds.push(failure.approvalId)
      }
      statusUnknownApprovalIds.push(failure.approvalId)
    } else {
      retryableApprovalIds.push(failure.approvalId)
      clearApprovalSubmissionUncertainty([failure.approvalId], result.attemptId)
    }
    return true
  })
  clearApprovalSubmissionUncertainty([
    ...result.succeededApprovalIds,
  ], result.attemptId)
  return {
    ...result,
    succeededApprovalIds,
    failures,
    retryableApprovalIds,
    recoverableApprovalIds,
    statusUnknownApprovalIds,
    ...(refreshError ? { refreshError } : {}),
  }
}
