import { useState } from "react"
import {
  CheckCircle,
  XCircle,
  Clock,
  Loader2,
  Brain,
  Wrench,
  ChevronDown,
} from "lucide-react"
import type { Step, ToolCall } from "../types"
import { ToolDetailPanel } from "./ToolDetailPanel"

interface StepCardProps {
  step: Step
  toolCall?: ToolCall | null
  label?: string
  compact?: boolean
  debug?: boolean
  defaultOpen?: boolean
}

export function StepCard({
  step,
  toolCall = null,
  label,
  compact = false,
  debug = false,
  defaultOpen = false,
}: StepCardProps) {
  const [open, setOpen] = useState(defaultOpen)
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
  const hasDetails = !compact && (toolCall || isLlmCall || error_message || debug)

  const statusIcon = () => {
    switch (status) {
      case "completed":
        return <CheckCircle className="w-4 h-4 flex-shrink-0 text-athena-success" />
      case "failed":
        return <XCircle className="w-4 h-4 flex-shrink-0 text-athena-danger" />
      case "running":
        return <Loader2 className="w-4 h-4 flex-shrink-0 text-athena-accent animate-spin" />
      default:
        return <Clock className="w-4 h-4 flex-shrink-0 text-athena-muted" />
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
      return <Brain className="w-3.5 h-3.5 flex-shrink-0 text-purple-400" />
    }
    return <Wrench className="w-3.5 h-3.5 flex-shrink-0 text-yellow-400" />
  }

  const stepTypeLabel = () => {
    if (isLlmCall) return debug ? "LLM Call" : "Model"
    return "Tool"
  }

  const displayTitle = () => {
    if (label) return label
    if (!isLlmCall) return toolCall?.tool_name ?? "Tool execution"
    if (debug) return "LLM Call"
    if (status === "running") return "Thinking"
    return "Composing"
  }

  const formatDuration = (ms: number) => {
    if (ms < 10) return `${ms.toFixed(2)}ms`
    if (ms < 1000) return `${Number(ms.toFixed(1))}ms`
    return `${Number((ms / 1000).toFixed(2))}s`
  }

  const formatTokens = (tokens: number) => {
    if (tokens < 1000) return tokens.toString()
    return `${(tokens / 1000).toFixed(1)}k`
  }

  const header = (
    <div className="px-3 py-2.5 bg-athena-bg/20">
      <div className="flex min-w-0 items-start gap-2">
        {statusIcon()}
        {stepTypeIcon()}
        <span className="mt-[-1px] font-mono text-sm font-medium text-athena-muted">
          {step_number}
        </span>
        <span className="min-w-0 flex-1 break-words text-sm font-medium leading-5 text-athena-text line-clamp-2">
          {displayTitle()}
        </span>
        {hasDetails && (
          <ChevronDown
            className={`mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-athena-muted transition-transform ${open ? "rotate-180" : ""}`}
          />
        )}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 pl-[4.75rem] text-xs text-athena-muted">
        <span>{stepTypeLabel()}</span>
        <span>{statusLabel()}</span>
        {duration_ms > 0 && (
          <span className="flex items-center gap-0.5">
            <Clock className="h-3 w-3 flex-shrink-0" />
            {formatDuration(duration_ms)}
          </span>
        )}
      </div>
    </div>
  )

  return (
    <div
      className={`overflow-hidden rounded-lg border border-athena-border/60 bg-athena-bg/25 transition-colors ${
        status === "running" ? "border-athena-accent/35" : ""
      } ${isLlmCall ? "border-l-purple-500/25" : "border-l-yellow-500/25"} border-l-2`}
    >
      {hasDetails ? (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="w-full border-b border-athena-border/45 text-left transition-colors hover:bg-athena-bg/45 focus:outline-none focus-visible:ring-1 focus-visible:ring-athena-accent/35"
        >
          {header}
        </button>
      ) : (
        <div className={compact ? "" : "border-b border-athena-border/45"}>{header}</div>
      )}

      {/* Body */}
      {!compact && open && (
        <div className="px-3 py-2 space-y-2">
          {toolCall && <ToolDetailPanel toolCall={toolCall} debug={debug} />}

          {/* LLM Token Usage */}
          {!toolCall && isLlmCall && (llm_input_tokens > 0 || llm_output_tokens > 0) && (
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
          <div className="flex flex-wrap items-center gap-3 text-xs text-athena-muted">
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
            {debug && (
              <span className="flex min-w-0 items-center gap-1">
                <span className="text-athena-text/60">Step ID:</span>
                <span className="font-mono truncate">{step.id}</span>
              </span>
            )}
          </div>

          {/* Error */}
          {!toolCall && error_message && (
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
