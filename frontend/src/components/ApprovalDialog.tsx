import { AlertTriangle, Check, Shield, X } from "lucide-react"
import { useEffect, useState } from "react"
import type { ApprovalRequest } from "../types"

interface ApprovalCardProps {
  requests: ApprovalRequest[]
  onSubmit: (decisions: Record<string, "approved" | "denied">) => void
}

function riskClass(risk: ApprovalRequest["risk_level"]): string {
  if (risk === "high") return "text-red-400 bg-red-500/20 border-red-500/50"
  if (risk === "medium") return "text-yellow-400 bg-yellow-500/20 border-yellow-500/50"
  return "text-green-400 bg-green-500/20 border-green-500/50"
}

export function ApprovalCard({ requests, onSubmit }: ApprovalCardProps) {
  const [resolving, setResolving] = useState<Set<string>>(new Set())
  const [decisions, setDecisions] = useState<Record<string, "approved" | "denied">>({})

  useEffect(() => {
    setResolving(new Set())
    setDecisions({})
  }, [requests.length])

  const resolve = (request: ApprovalRequest, action: "allow" | "deny") => {
    const next = { ...decisions, [request.approval_id]: action === "allow" ? "approved" : "denied" }
    setDecisions(next)
    setResolving((current) => new Set(current).add(request.approval_id))
    if (Object.keys(next).length === requests.length) onSubmit(next)
  }

  const resolveAll = (action: "allow" | "deny") => {
    const next = Object.fromEntries(requests.map((request) => [request.approval_id, action === "allow" ? "approved" : "denied"])) as Record<string, "approved" | "denied">
    setDecisions(next)
    setResolving(new Set(requests.map((request) => request.approval_id)))
    onSubmit(next)
  }

  return (
    <section className="card border-yellow-500/40 shadow-lg" aria-live="polite">
      <div className="flex items-center gap-3 border-b border-athena-border px-4 py-3">
        <div className="rounded-lg border border-yellow-500/50 bg-yellow-500/20 p-2 text-yellow-400">
          <Shield className="h-5 w-5" />
        </div>
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-athena-text">
            Approval required
            <span className="rounded-full bg-athena-bg px-2 py-0.5 text-xs font-normal text-athena-muted">
              {requests.length} {requests.length === 1 ? "request" : "requests"}
            </span>
          </h3>
          <p className="text-xs text-athena-muted">Review these actions before Athena continues.</p>
        </div>
      </div>

      <div className="divide-y divide-athena-border/70">
        {requests.map((request) => {
          const isResolving = resolving.has(request.approval_id)
          return (
            <div key={request.approval_id} className="px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <AlertTriangle className="h-4 w-4 text-yellow-400" />
                <code className="text-sm text-athena-accent">{request.tool_name}</code>
                <span className={`rounded border px-2 py-0.5 text-[11px] font-medium ${riskClass(request.risk_level)}`}>
                  {request.risk_level.toUpperCase()}
                </span>
                <div className="ml-auto flex gap-2">
                  <button
                    type="button"
                    className="btn-secondary px-2.5 py-1.5 text-xs"
                    disabled={isResolving}
                    onClick={() => resolve(request, "deny")}
                  >
                    <X className="h-3.5 w-3.5" /> Deny
                  </button>
                  <button
                    type="button"
                    className="btn-primary px-2.5 py-1.5 text-xs"
                    disabled={isResolving}
                    onClick={() => resolve(request, "allow")}
                  >
                    <Check className="h-3.5 w-3.5" /> Allow
                  </button>
                </div>
              </div>
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-athena-muted hover:text-athena-text">
                  Arguments
                </summary>
                <pre className="mt-2 max-h-36 overflow-auto rounded bg-athena-bg p-2 text-xs text-athena-text/90">
                  {JSON.stringify(request.arguments, null, 2)}
                </pre>
              </details>
            </div>
          )
        })}
      </div>

      {requests.length > 1 && (
        <div className="flex justify-end gap-2 border-t border-athena-border px-4 py-3">
          <button type="button" className="btn-secondary text-xs" onClick={() => resolveAll("deny")}>
            <X className="h-3.5 w-3.5" /> Deny all
          </button>
          <button type="button" className="btn-primary text-xs" onClick={() => resolveAll("allow")}>
            <Check className="h-3.5 w-3.5" /> Allow all
          </button>
        </div>
      )}
    </section>
  )
}
