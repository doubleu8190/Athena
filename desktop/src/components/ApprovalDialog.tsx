import { AlertTriangle, Clock, X, Check, Shield } from "lucide-react"
import { useState, useEffect } from "react"
import type { ApprovalRequest } from "../types"

interface ApprovalDialogProps {
  request: ApprovalRequest
  onApprove: () => void
  onDeny: () => void
  onCancel: () => void
}

export function ApprovalDialog({
  request,
  onApprove,
  onDeny,
  onCancel,
}: ApprovalDialogProps) {
  const { tool_name, arguments: args, risk_level, timeout } = request
  const [remaining, setRemaining] = useState(timeout)
  const [collapsed, setCollapsed] = useState(false)

  useEffect(() => {
    setRemaining(timeout)
    const interval = setInterval(() => {
      setRemaining((prev) => {
        if (prev <= 1) {
          clearInterval(interval)
          return 0
        }
        return prev - 1
      })
    }, 1000)
    return () => clearInterval(interval)
  }, [timeout])

  const riskColor =
    risk_level === "high"
      ? "text-red-400 bg-red-500/20 border-red-500/50"
      : risk_level === "medium"
        ? "text-yellow-400 bg-yellow-500/20 border-yellow-500/50"
        : "text-green-400 bg-green-500/20 border-green-500/50"

  const urgencyClass =
    remaining <= 10 ? "animate-pulse" : remaining <= 30 ? "animate-pulse-slow" : ""

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div
        className={`card w-full max-w-md shadow-2xl animate-slide-up ${urgencyClass}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-athena-border flex items-center gap-3">
          <div className={`p-2 rounded-lg ${riskColor} border`}>
            <AlertTriangle className="w-5 h-5" />
          </div>
          <div>
            <h3 className="font-semibold text-base flex items-center gap-2">
              <Shield className="w-4 h-4" />
              Approval Required
            </h3>
            <p className="text-xs text-athena-muted">
              This action requires your confirmation
            </p>
          </div>
          <button
            onClick={onCancel}
            className="ml-auto text-athena-muted hover:text-athena-text transition-colors"
            title="Cancel"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="px-4 py-3 space-y-3">
          <div className="flex items-center gap-3 text-sm">
            <span className="text-athena-muted">Tool:</span>
            <code className="bg-athena-bg px-2 py-0.5 rounded text-athena-accent">
              {tool_name}
            </code>
            <span
              className={`ml-auto px-2 py-0.5 rounded text-xs font-medium ${riskColor}`}
            >
              {risk_level.toUpperCase()} RISK
            </span>
          </div>

          {/* Arguments */}
          <div>
            <button
              className="text-xs text-athena-muted hover:text-athena-text transition-colors flex items-center gap-1"
              onClick={() => setCollapsed(!collapsed)}
            >
              {collapsed ? "▶" : "▼"} Arguments
            </button>
            {!collapsed && (
              <pre className="mt-2 text-xs bg-athena-bg rounded p-3 overflow-x-auto text-athena-text/90 max-h-48 overflow-y-auto">
                {JSON.stringify(args, null, 2)}
              </pre>
            )}
          </div>

          {/* Description */}
          {request.description && (
            <p className="text-sm text-athena-text/80 bg-athena-bg/50 rounded p-2">
              {request.description}
            </p>
          )}

          {/* Timer */}
          <div className="flex items-center gap-2 text-xs text-athena-muted">
            <Clock className="w-3 h-3" />
            <span
              className={
                remaining <= 10 ? "text-athena-danger font-medium" : ""
              }
            >
              Timeout in {remaining}s
            </span>
          </div>
        </div>

        {/* Footer */}
        <div className="px-4 py-3 border-t border-athena-border flex gap-2 justify-end">
          <button onClick={onDeny} className="btn-secondary">
            <X className="w-4 h-4" />
            Deny
          </button>
          <button onClick={onApprove} className="btn-primary">
            <Check className="w-4 h-4" />
            Allow
          </button>
        </div>
      </div>
    </div>
  )
}
