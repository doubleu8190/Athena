import { AlertTriangle, Check, Shield, X, Loader2 } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import type { ApprovalRequest, ApprovalStatusSnapshot } from "../types"
import type { ApprovalSubmissionResult } from "../api/client"
import { approvalActionDescription } from "../utils/executionCopy"
import {
  approvalIdFromStorageKey,
  clearApprovalSubmissionUncertainty,
  getConflictingUncertainApprovalIds,
  getUncertainApprovalDecisions,
  getUncertainApprovalAttempts,
  getUncertainApprovalIds,
  getSubmittableApprovalRequests,
  isApprovalBatchComplete,
  markApprovalSubmissionsUncertain,
  retainApprovalIds,
} from "../utils/approvalSubmission"

interface ApprovalCardProps {
  batchId: string
  requests: ApprovalRequest[]
  onSubmit: (batchId: string, requests: ApprovalRequest[], decisions: Record<string, "approved" | "denied" | "cancelled">, attemptId: string) => Promise<ApprovalSubmissionResult>
  onRefreshStatus?: (requests: ApprovalRequest[]) => Promise<ReadonlyMap<string, ApprovalStatusSnapshot>>
}

function riskClass(risk: ApprovalRequest["risk_level"]): string {
  if (risk === "critical") return "text-fuchsia-300 bg-fuchsia-500/20 border-fuchsia-500/50"
  if (risk === "high") return "text-red-400 bg-red-500/20 border-red-500/50"
  if (risk === "medium") return "text-yellow-400 bg-yellow-500/20 border-yellow-500/50"
  return "text-green-400 bg-green-500/20 border-green-500/50"
}

export function ApprovalCard({ batchId, requests, onSubmit, onRefreshStatus }: ApprovalCardProps) {
  const requestIds = requests.map((request) => request.approval_id).join(":")
  const previousBatchId = useRef(batchId)
  const refreshStatusRef = useRef(onRefreshStatus)
  refreshStatusRef.current = onRefreshStatus
  const initialUncertainDecisions = getUncertainApprovalDecisions()
  const initialUncertainIds = getUncertainApprovalIds()
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [submittedApprovalIds, setSubmittedApprovalIds] = useState<Set<string>>(() => new Set())
  const [blockedApprovalIds, setBlockedApprovalIds] = useState<Set<string>>(() =>
    new Set(requests.map((request) => request.approval_id).filter((id) => initialUncertainIds.has(id))),
  )
  const [recoverableApprovalIds, setRecoverableApprovalIds] = useState<Set<string>>(() => new Set())
  const [arbitrableApprovalIds, setArbitrableApprovalIds] = useState<Set<string>>(() => new Set())
  const [submissionFailures, setSubmissionFailures] = useState<Record<string, string>>({})
  const [refreshError, setRefreshError] = useState<string | null>(null)
  const [refreshingStatus, setRefreshingStatus] = useState(false)
  const [statusUnknownApprovalIds, setStatusUnknownApprovalIds] = useState<Set<string>>(() =>
    new Set(requests.map((request) => request.approval_id).filter((id) => initialUncertainIds.has(id))),
  )
  const [decisions, setDecisions] = useState<Record<string, "approved" | "denied" | "cancelled">>(() =>
    Object.fromEntries(requests.flatMap((request) => {
      const decision = initialUncertainDecisions.get(request.approval_id)
      return decision ? [[request.approval_id, decision]] : []
    })),
  )

  const refreshUnknownStatus = async (active = () => true) => {
    const uncertainDecisions = getUncertainApprovalDecisions()
    const uncertainIds = getUncertainApprovalIds()
    const uncertainRequests = requests.filter((request) => uncertainIds.has(request.approval_id))
    if (!uncertainRequests.length || !refreshStatusRef.current) return
    setRefreshingStatus(true)
    try {
      const statusById = await refreshStatusRef.current(uncertainRequests)
      if (uncertainRequests.some((request) => !statusById.has(request.approval_id))) {
        throw new Error("Approval status response was incomplete")
      }
      const stillPendingIds = new Set(uncertainRequests
        .map((request) => request.approval_id)
        .filter((approvalId) => statusById.get(approvalId)?.resolved === false))
      const conflictingIds = getConflictingUncertainApprovalIds()
      const pendingArbitrableIds = new Set([...stillPendingIds].filter((id) => conflictingIds.has(id)))
      const resolvedIds = uncertainRequests
        .map((request) => request.approval_id)
        .filter((approvalId) => statusById.get(approvalId)?.resolved === true)
      const confirmedDecisionIds = new Set(resolvedIds.filter((approvalId) =>
        statusById.get(approvalId)?.resolution === uncertainDecisions.get(approvalId),
      ))
      const otherDecisionFailures = Object.fromEntries(resolvedIds
        .filter((approvalId) => !confirmedDecisionIds.has(approvalId))
        .map((approvalId) => [
          approvalId,
          `已由其他操作处理（${statusById.get(approvalId)?.resolution ?? "状态未知"}）`,
        ]))
      clearApprovalSubmissionUncertainty(resolvedIds)
      if (!active()) return
      setSubmittedApprovalIds((current) => new Set([...current, ...confirmedDecisionIds]))
      setSubmissionFailures((current) => ({ ...current, ...otherDecisionFailures }))
      setSubmitted(requests.length > 0 && requests.every((request) =>
        confirmedDecisionIds.has(request.approval_id) || submittedApprovalIds.has(request.approval_id),
      ))
      setBlockedApprovalIds((current) => new Set([
        ...[...current].filter((approvalId) => !resolvedIds.includes(approvalId)),
        ...Object.keys(otherDecisionFailures),
      ]))
      setStatusUnknownApprovalIds(stillPendingIds)
      setRecoverableApprovalIds(new Set([...stillPendingIds].filter((approvalId) => uncertainDecisions.has(approvalId))))
      setArbitrableApprovalIds(pendingArbitrableIds)
      setRefreshError(null)
    } catch (error) {
      if (active()) setRefreshError(error instanceof Error ? error.message : "Approval status refresh failed")
    } finally {
      if (active()) setRefreshingStatus(false)
    }
  }

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if (!event.key) return
      const approvalId = approvalIdFromStorageKey(event.key)
      if (!approvalId) return
      const request = requests.find((item) => item.approval_id === approvalId)
      if (!request) return
      if (event.newValue === "approved" || event.newValue === "denied") {
        const attempts = getUncertainApprovalAttempts().get(approvalId)
        const lockedDecision = getUncertainApprovalDecisions().get(approvalId)
        setDecisions((current) => {
          const next = { ...current }
          if (lockedDecision) next[approvalId] = lockedDecision
          else delete next[approvalId]
          return next
        })
        setBlockedApprovalIds((current) => new Set([...current, approvalId]))
        setStatusUnknownApprovalIds((current) => new Set([...current, approvalId]))
        setRecoverableApprovalIds((current) => {
          const next = new Set(current)
          if (attempts?.size && lockedDecision) next.add(approvalId)
          else next.delete(approvalId)
          return next
        })
        void refreshUnknownStatus()
        return
      }
      const refresh = refreshStatusRef.current
      if (!refresh) return
      setRefreshingStatus(true)
      void refresh([request]).then((statusById) => {
        const status = statusById.get(approvalId)
        if (!status) throw new Error("Approval status response was incomplete")
        const remainingAttempts = getUncertainApprovalAttempts().get(approvalId)
        const remainingDecision = getUncertainApprovalDecisions().get(approvalId)
        if (status.resolved) {
          const allPendingAttemptsAgree = remainingAttempts
            ? new Set(remainingAttempts.values()).size === 1
            : false
          if (allPendingAttemptsAgree && status.resolution === remainingDecision) {
            setSubmittedApprovalIds((current) => new Set([...current, approvalId]))
          } else {
            setSubmissionFailures((current) => ({
              ...current,
              [approvalId]: `已由其他操作处理（${status.resolution}）`,
            }))
            setBlockedApprovalIds((current) => new Set([...current, approvalId]))
          }
          clearApprovalSubmissionUncertainty([approvalId])
        } else {
          // A pending snapshot cannot prove that a removed storage key's
          // request was harmless: it may have been in flight when the key
          // was cleared. Keep the local approval locked until a terminal
          // status is authoritative.
          setBlockedApprovalIds((current) => new Set([...current, approvalId]))
          setStatusUnknownApprovalIds((current) => new Set([...current, approvalId]))
          setDecisions((current) => {
            const next = { ...current }
            if (remainingDecision) next[approvalId] = remainingDecision
            else delete next[approvalId]
            return next
          })
          setRecoverableApprovalIds((current) => {
            const next = new Set(current)
            if (remainingAttempts?.size && remainingDecision) next.add(approvalId)
            else next.delete(approvalId)
            return next
          })
          setArbitrableApprovalIds((current) => {
            const next = new Set(current)
            if (getConflictingUncertainApprovalIds().has(approvalId)) next.add(approvalId)
            return next
          })
        }
      }).catch((error: unknown) => {
        setRefreshError(error instanceof Error ? error.message : "Approval status refresh failed")
      }).finally(() => setRefreshingStatus(false))
    }
    window.addEventListener("storage", handleStorage)
    return () => window.removeEventListener("storage", handleStorage)
  }, [requestIds])

  useEffect(() => {
    const batchChanged = previousBatchId.current !== batchId
    previousBatchId.current = batchId
    if (batchChanged) {
      const uncertainDecisions = getUncertainApprovalDecisions()
      const uncertainIds = getUncertainApprovalIds()
      const blockedIds = new Set(requests.map((request) => request.approval_id).filter((id) => uncertainIds.has(id)))
      setDecisions(Object.fromEntries(requests.flatMap((request) => {
        const decision = uncertainDecisions.get(request.approval_id)
        return decision ? [[request.approval_id, decision]] : []
      })))
      setSubmitting(false)
      setSubmitted(false)
      setSubmittedApprovalIds(new Set())
      setBlockedApprovalIds(blockedIds)
      setRecoverableApprovalIds(new Set())
      setArbitrableApprovalIds(new Set())
      setSubmissionFailures({})
      setRefreshError(null)
      setStatusUnknownApprovalIds(blockedIds)
    } else {
      const presentIds = new Set(requests.map((request) => request.approval_id))
      const retainedSubmittedIds = retainApprovalIds(submittedApprovalIds, presentIds)
      const uncertainIds = getUncertainApprovalIds()
      const retainedBlockedIds = new Set([
        ...retainApprovalIds(blockedApprovalIds, presentIds),
        ...retainApprovalIds(uncertainIds, presentIds),
      ])
      setDecisions((current) => Object.fromEntries(Object.entries(current).filter(([id]) => presentIds.has(id))))
      setSubmittedApprovalIds(retainedSubmittedIds)
      setSubmitted(requests.length > 0 && requests.every((request) => retainedSubmittedIds.has(request.approval_id)))
      setBlockedApprovalIds(retainedBlockedIds)
      setRecoverableApprovalIds((current) => retainApprovalIds(current, presentIds))
      setSubmissionFailures((current) => Object.fromEntries(Object.entries(current).filter(([id]) => id === "batch" || presentIds.has(id))))
      setStatusUnknownApprovalIds(retainApprovalIds(statusUnknownApprovalIds, retainedBlockedIds))
    }
    let active = true
    void refreshUnknownStatus(() => active)
    return () => { active = false }
  }, [batchId, requestIds])

  const submit = async (next = decisions) => {
    const pendingRequests = getSubmittableApprovalRequests(
      requests,
      submittedApprovalIds,
      blockedApprovalIds,
      recoverableApprovalIds,
      arbitrableApprovalIds,
      next,
    )
    if (submitting || refreshingStatus || pendingRequests.length === 0 || pendingRequests.some((request) => !next[request.approval_id])) return
    setSubmitting(true)
    const attemptId = markApprovalSubmissionsUncertain(Object.fromEntries(pendingRequests.map((request) => [request.approval_id, next[request.approval_id] as "approved" | "denied"])))
    try {
      const result = await onSubmit(
        batchId,
        pendingRequests,
        Object.fromEntries(pendingRequests.map((request) => [request.approval_id, next[request.approval_id]])),
        attemptId,
      )
      setSubmittedApprovalIds((current) => new Set([...current, ...result.succeededApprovalIds]))
      clearApprovalSubmissionUncertainty([
        ...result.succeededApprovalIds,
        ...result.failures
          .map((failure) => failure.approvalId)
          .filter((approvalId) => !result.statusUnknownApprovalIds.includes(approvalId)),
      ], result.attemptId)
      setBlockedApprovalIds((current) => {
        const next = new Set(current)
        for (const approvalId of result.retryableApprovalIds) next.delete(approvalId)
        for (const approvalId of result.statusUnknownApprovalIds) next.add(approvalId)
        for (const failure of result.failures) {
          if (!result.retryableApprovalIds.includes(failure.approvalId)) next.add(failure.approvalId)
        }
        return next
      })
      setSubmissionFailures(Object.fromEntries(result.failures.map((failure) => [failure.approvalId, failure.message])))
      setRefreshError(result.refreshError ?? null)
      setStatusUnknownApprovalIds(new Set(result.statusUnknownApprovalIds))
      setRecoverableApprovalIds(new Set(result.recoverableApprovalIds))
      if (result.statusUnknownApprovalIds.length > 0) {
        setBlockedApprovalIds((current) => new Set([...current, ...result.statusUnknownApprovalIds]))
      }
      const confirmedApprovalIds = new Set([
        ...submittedApprovalIds,
        ...result.succeededApprovalIds,
      ])
      setSubmitted(isApprovalBatchComplete(requests, confirmedApprovalIds))
      setSubmitting(false)
    } catch (error) {
      setSubmissionFailures({ batch: error instanceof Error ? error.message : "Approval response failed" })
      setStatusUnknownApprovalIds(new Set(requests.filter((request) => !submittedApprovalIds.has(request.approval_id)).map((request) => request.approval_id)))
      setRecoverableApprovalIds(new Set())
      setBlockedApprovalIds((current) => new Set([
        ...current,
        ...requests.filter((request) => !submittedApprovalIds.has(request.approval_id)).map((request) => request.approval_id),
      ]))
      setSubmitting(false)
    }
  }

  const decideAll = (decision: "approved" | "denied" | "cancelled") => {
    const next = Object.fromEntries(requests.filter((request) => !submittedApprovalIds.has(request.approval_id) && (!blockedApprovalIds.has(request.approval_id) || arbitrableApprovalIds.has(request.approval_id))).map((request) => [request.approval_id, decision])) as Record<string, "approved" | "denied" | "cancelled">
    setDecisions((current) => ({ ...current, ...next }))
  }

  const visibleUnknownIds = new Set([
    ...statusUnknownApprovalIds,
    ...requests.map((request) => request.approval_id).filter((id) => getUncertainApprovalIds().has(id)),
  ])

  return (
    <section className="chat-approval-card card border-yellow-500/40 shadow-lg" aria-live="polite">
      <div className="flex items-center gap-3 border-b border-athena-border px-4 py-3">
        <div className="rounded-lg border border-yellow-500/50 bg-yellow-500/20 p-2 text-yellow-400">
          <Shield className="h-5 w-5" />
        </div>
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-athena-text">
            需要你确认操作
            <span className="rounded-full bg-athena-bg px-2 py-0.5 text-xs font-normal text-athena-muted">
              {requests.length} 项
            </span>
          </h3>
          <p className="text-xs text-athena-muted">请确认是否允许 Athena 继续执行。</p>
        </div>
      </div>

      <div className="divide-y divide-athena-border/70">
        {requests.map((request) => {
          const decision = decisions[request.approval_id]
          return (
            <div key={request.approval_id} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <AlertTriangle className="h-4 w-4 text-yellow-400" />
                <div className="min-w-0">
                  {request.task_title && <div className="text-xs text-athena-warning">任务「{request.task_title}」需要确认</div>}
                  <span className="text-sm font-medium text-athena-text">{request.description || approvalActionDescription(request.tool_name, request.arguments)}</span>
                  {request.expires_at && <span className="text-xs text-athena-muted">到期后自动拒绝 · {new Date(request.expires_at).toLocaleString("zh-CN")}</span>}
                </div>
                <span className={`rounded border px-2 py-0.5 text-[11px] font-medium ${riskClass(request.risk_level)}`}>
                  {request.risk_level === "critical" ? "严重风险" : request.risk_level === "high" ? "高风险" : request.risk_level === "medium" ? "中风险" : "低风险"}
                </span>
                <div className="ml-auto flex gap-2">
                  <button type="button" className={`btn-secondary px-2.5 py-1.5 text-xs ${decision === "denied" ? "border-athena-danger text-athena-danger" : ""}`} disabled={submitting || refreshingStatus || submittedApprovalIds.has(request.approval_id) || (blockedApprovalIds.has(request.approval_id) && !arbitrableApprovalIds.has(request.approval_id))} onClick={() => setDecisions((current) => ({ ...current, [request.approval_id]: "denied" }))}>
                    <X className="h-3.5 w-3.5" /> 拒绝
                  </button>
                  <button type="button" className={`btn-primary px-2.5 py-1.5 text-xs ${decision === "approved" ? "ring-2 ring-green-400" : ""}`} disabled={submitting || refreshingStatus || submittedApprovalIds.has(request.approval_id) || (blockedApprovalIds.has(request.approval_id) && !arbitrableApprovalIds.has(request.approval_id))} onClick={() => setDecisions((current) => ({ ...current, [request.approval_id]: "approved" }))}>
                    <Check className="h-3.5 w-3.5" /> 允许
                  </button>
                </div>
              </div>
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-athena-muted hover:text-athena-text">
                  查看操作参数
                </summary>
                <div className="mt-2 space-y-2">
                  <div className="text-[11px] text-athena-muted">工具标识：<code className="break-all">{request.tool_name}</code></div>
                  <pre className="max-h-36 overflow-auto whitespace-pre-wrap break-all rounded bg-athena-bg p-2 text-xs text-athena-text/90">
                    {JSON.stringify(request.arguments, null, 2)}
                  </pre>
                </div>
              </details>
            </div>
          )
        })}
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-athena-border px-4 py-3">
        <div className="flex gap-2">
          <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={submitting || submitted} onClick={() => decideAll("denied")}>全部拒绝</button>
          <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={submitting || submitted} onClick={() => decideAll("approved")}>全部允许</button>
        </div>
        <button type="button" className="btn-primary text-xs" disabled={submitting || refreshingStatus || submitted || !getSubmittableApprovalRequests(requests, submittedApprovalIds, blockedApprovalIds, recoverableApprovalIds, arbitrableApprovalIds, decisions).length || requests.some((request) => !submittedApprovalIds.has(request.approval_id) && (!blockedApprovalIds.has(request.approval_id) || arbitrableApprovalIds.has(request.approval_id)) && !decisions[request.approval_id])} onClick={() => void submit()}>
          {submitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
          {submitting ? "提交中" : submitted ? "已提交" : arbitrableApprovalIds.size > 0 ? "重新选择并仲裁" : recoverableApprovalIds.size > 0 ? "重试原决定" : "提交决策"}
        </button>
      </div>
      {(Object.keys(submissionFailures).length > 0 || refreshError) && (
        <div className="border-t border-athena-border px-4 py-3 text-xs text-athena-danger" role="status">
          {refreshError && <p>{refreshError}</p>}
          {Object.keys(submissionFailures).length > 0 && (
            <p>{submissionFailures.batch
              ? `提交状态未知：${submissionFailures.batch}。已锁定这些项目，请刷新权威审批状态后再操作。`
              : `部分决策未提交：${Object.values(submissionFailures).join("；")}。已成功提交的项目不会重复发送。${statusUnknownApprovalIds.size > 0 ? "状态未确认的项目已锁定，请刷新审批状态后再操作。" : "仍处于待审批状态的失败项目可以重试。"}`}</p>
          )}
        </div>
      )}
      {visibleUnknownIds.size > 0 && (
        <div className="border-t border-athena-border px-4 py-3 text-xs text-athena-warning" role="status">
          提交结果尚未确认。为避免更改已发送的决定，这些项目已锁定。
          {arbitrableApprovalIds.size > 0 ? " 检测到相互冲突的未确认决定；可重新选择后提交，由服务端按先完成的决定仲裁，最终结果以刷新状态为准。" : null}
          {recoverableApprovalIds.size > 0
            ? " 权威状态仍显示待审批；可以使用原决定安全重试。"
            : " 可刷新审批状态；确认仍待审批后，可以使用原决定安全重试。"}
          {recoverableApprovalIds.size === 0 && onRefreshStatus && (
            <button type="button" className="ml-2 underline" disabled={refreshingStatus} onClick={() => void refreshUnknownStatus()}>
              {refreshingStatus ? "刷新中" : "刷新审批状态"}
            </button>
          )}
        </div>
      )}
    </section>
  )
}
