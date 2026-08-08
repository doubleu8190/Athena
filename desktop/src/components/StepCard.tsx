import {
  CheckCircle,
  XCircle,
  Clock,
  Loader2,
  Brain,
  Wrench,
} from "lucide-react"
import type { Step } from "../types"

interface StepCardProps {
  step: Step
  compact?: boolean
}

export function StepCard({ step, compact = false }: StepCardProps) {
  const {
    step_type,
    status,
    step_number,
    duration_ms,
    llm_input_tokens,
    llm_output_tokens,
    error_message,
    started_at,
    completed_at,
  } = step

  const isLlmCall = step_type === "llm_call"

  const statusIcon = () => {
    switch (status) {
      case "completed":
        return <CheckCircle className="w-4 h-4 text-athena-success" />
      case "failed":
        return <XCircle className="w-4 h-4 text-athena-danger" />
      case "running":
        return <Loader2 className="w-4 h-4 text-athena-accent animate-spin" />
      default:
        return <Clock className="w-4 h-4 text-athena-muted" />
    }
  }

  const statusLabel = () => {
    switch (status) {
      case "completed":
        return "Completed"
      case "failed":
        return "Failed"
      case "running":
        return "Running"
      default:
        return "Pending"
    }
  }

  const stepTypeIcon = () => {
    if (isLlmCall) {
      return <Brain className="w-3.5 h-3.5 text-purple-400" />
    }
    return <Wrench className="w-3.5 h-3.5 text-yellow-400" />
  }

  const stepTypeLabel = () => {
    if (isLlmCall) return "LLM Call"
    return "Tool Execution"
  }

  const formatDuration = (ms: number) => {
    if (ms < 1000) return `${ms}ms`
    return `${(ms / 1000).toFixed(1)}s`
  }

  const formatTokens = (tokens: number) => {
    if (tokens < 1000) return tokens.toString()
    return `${(tokens / 1000).toFixed(1)}k`
  }

  return (
    <div
      className={`card overflow-hidden transition-all ${
        status === "running" ? "border-athena-accent/50" : ""
      } ${isLlmCall ? "border-l-2 border-l-purple-500/30" : "border-l-2 border-l-yellow-500/30"}`}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 bg-athena-bg/50 border-b border-athena-border">
        {statusIcon()}
        <div className="flex items-center gap-1.5">
          {stepTypeIcon()}
          <span className="font-mono text-sm font-medium">
            Step {step_number}
          </span>
          <span className="text-xs text-athena-muted">
            ({stepTypeLabel()})
          </span>
        </div>
        <span className="ml-auto text-xs text-athena-muted flex items-center gap-1">
          {statusLabel()}
          {duration_ms > 0 && (
            <span className="flex items-center gap-0.5">
              <Clock className="w-3 h-3" />
              {formatDuration(duration_ms)}
            </span>
          )}
        </span>
      </div>

      {/* Body */}
      {!compact && (
        <div className="px-3 py-2 space-y-2">
          {/* LLM Token Usage */}
          {isLlmCall && (llm_input_tokens > 0 || llm_output_tokens > 0) && (
            <div className="flex items-center gap-3 text-xs text-athena-muted">
              <span className="flex items-center gap-1">
                <span className="text-athena-text/60">Input:</span>
                <span className="font-mono">{formatTokens(llm_input_tokens)}</span>
              </span>
              <span className="flex items-center gap-1">
                <span className="text-athena-text/60">Output:</span>
                <span className="font-mono">{formatTokens(llm_output_tokens)}</span>
              </span>
            </div>
          )}

          {/* Timing Info */}
          <div className="flex items-center gap-3 text-xs text-athena-muted">
            <span className="flex items-center gap-1">
              <span className="text-athena-text/60">Started:</span>
              <span className="font-mono">{formatTime(started_at)}</span>
            </span>
            {completed_at && (
              <span className="flex items-center gap-1">
                <span className="text-athena-text/60">Completed:</span>
                <span className="font-mono">{formatTime(completed_at)}</span>
              </span>
            )}
          </div>

          {/* Error */}
          {error_message && (
            <div className="text-xs text-athena-danger bg-red-500/10 rounded p-2">
              {error_message}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function formatTime(isoString: string): string {
  try {
    const date = new Date(isoString)
    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
  } catch {
    return ""
  }
}
