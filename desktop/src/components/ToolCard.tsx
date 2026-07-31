import { CheckCircle, XCircle, Clock, AlertTriangle, Loader2 } from "lucide-react"
import type { ToolCall } from "../types"

interface ToolCardProps {
  toolCall: ToolCall
  compact?: boolean
}

export function ToolCard({ toolCall, compact = false }: ToolCardProps) {
  const { tool_name, arguments: args, status, output, error, duration_ms, risk_level } =
    toolCall

  const statusIcon = () => {
    switch (status) {
      case "success":
        return <CheckCircle className="w-4 h-4 text-athena-success" />
      case "failed":
        return <XCircle className="w-4 h-4 text-athena-danger" />
      case "denied":
      case "timeout":
        return <AlertTriangle className="w-4 h-4 text-athena-warning" />
      case "running":
      default:
        return <Loader2 className="w-4 h-4 text-athena-accent animate-spin" />
    }
  }

  const statusLabel = () => {
    switch (status) {
      case "success":
        return "Success"
      case "failed":
        return "Failed"
      case "denied":
        return "Denied"
      case "timeout":
        return "Timeout"
      case "running":
        return "Running"
      default:
        return "Pending"
    }
  }

  const riskBadge = () => {
    const className =
      risk_level === "high"
        ? "risk-high"
        : risk_level === "medium"
          ? "risk-medium"
          : "risk-low"
    return <span className={`risk-badge ${className}`}>{risk_level.toUpperCase()}</span>
  }

  return (
    <div
      className={`card overflow-hidden transition-all ${
        status === "running" ? "border-athena-accent/50" : ""
      }`}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 bg-athena-bg/50 border-b border-athena-border">
        {statusIcon()}
        <span className="font-mono text-sm font-medium">{tool_name}</span>
        {riskBadge()}
        <span className="ml-auto text-xs text-athena-muted flex items-center gap-1">
          {statusLabel()}
          {duration_ms != null && (
            <span className="flex items-center gap-0.5">
              <Clock className="w-3 h-3" />
              {(duration_ms / 1000).toFixed(1)}s
            </span>
          )}
        </span>
      </div>

      {/* Body */}
      {!compact && (
        <div className="px-3 py-2 space-y-2">
          {/* Arguments */}
          {args && Object.keys(args).length > 0 && (
            <details className="group">
              <summary className="cursor-pointer text-xs text-athena-muted hover:text-athena-text select-none">
                Arguments ({Object.keys(args).length})
              </summary>
              <pre className="mt-2 text-xs bg-athena-bg rounded p-2 overflow-x-auto text-athena-text/80">
                {JSON.stringify(args, null, 2)}
              </pre>
            </details>
          )}

          {/* Output */}
          {output && status === "success" && (
            <details className="group">
              <summary className="cursor-pointer text-xs text-athena-muted hover:text-athena-text select-none">
                Output
              </summary>
              <pre className="mt-2 text-xs bg-athena-bg rounded p-2 overflow-x-auto text-athena-text/80 max-h-48 overflow-y-auto">
                {output.length > 1000 ? output.slice(0, 1000) + "..." : output}
              </pre>
            </details>
          )}

          {/* Error */}
          {error && (
            <div className="text-xs text-athena-danger bg-red-500/10 rounded p-2">
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
