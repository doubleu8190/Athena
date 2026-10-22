import { AlertTriangle, Check, Shield, X, Loader2 } from "lucide-react"
import { useEffect, useState } from "react"
import type { ApprovalRequest } from "../types"
import { approvalActionDescription } from "../utils/executionCopy"

interface ApprovalCardProps {
  batchId: string
  requests: ApprovalRequest[]
  onSubmit: (batchId: string, requests: ApprovalRequest[], decisions: Record<string, "approved" | "denied" | "cancelled">) => Promise<void>
}

function riskClass(risk: ApprovalRequest["risk_level"]): string {
  if (risk === "high") return "text-red-400 bg-red-500/20 border-red-500/50"
  if (risk === "medium") return "text-yellow-400 bg-yellow-500/20 border-yellow-500/50"
  return "text-green-400 bg-green-500/20 border-green-500/50"
}

export function ApprovalCard({ batchId, requests, onSubmit }: ApprovalCardProps) {
  const [submitting, setSubmitting] = useState(false)
  const [decisions, setDecisions] = useState<Record<string, "approved" | "denied" | "cancelled">>({})

  useEffect(() => {
    setDecisions({})
    setSubmitting(false)
  }, [batchId, requests.map((request) => request.approval_id).join(":")])

  const submit = async (next = decisions) => {
    if (submitting || requests.some((request) => !next[request.approval_id])) return
    setSubmitting(true)
    try {
      await onSubmit(batchId, requests, next)
    } catch {
      setSubmitting(false)
    }
  }

  const decideAll = (decision: "approved" | "denied" | "cancelled") => {
    const next = Object.fromEntries(requests.map((request) => [request.approval_id, decision])) as Record<string, "approved" | "denied" | "cancelled">
    setDecisions(next)
  }

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
                  {request.risk_level === "high" ? "高风险" : request.risk_level === "medium" ? "中风险" : "低风险"}
                </span>
                <div className="ml-auto flex gap-2">
                  <button type="button" className={`btn-secondary px-2.5 py-1.5 text-xs ${decision === "denied" ? "border-athena-danger text-athena-danger" : ""}`} disabled={submitting} onClick={() => setDecisions((current) => ({ ...current, [request.approval_id]: "denied" }))}>
                    <X className="h-3.5 w-3.5" /> 拒绝
                  </button>
                  <button type="button" className={`btn-primary px-2.5 py-1.5 text-xs ${decision === "approved" ? "ring-2 ring-green-400" : ""}`} disabled={submitting} onClick={() => setDecisions((current) => ({ ...current, [request.approval_id]: "approved" }))}>
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
          <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={submitting} onClick={() => decideAll("denied")}>全部拒绝</button>
          <button type="button" className="btn-ghost px-2 py-1 text-xs" disabled={submitting} onClick={() => decideAll("approved")}>全部允许</button>
        </div>
        <button type="button" className="btn-primary text-xs" disabled={submitting || requests.some((request) => !decisions[request.approval_id])} onClick={() => void submit()}>
          {submitting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
          {submitting ? "提交中" : "提交决策"}
        </button>
      </div>
    </section>
  )
}
