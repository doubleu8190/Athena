import { useState } from "react"
import {
  AlertTriangle,
  CheckCircle,
  ChevronDown,
  Clock,
  Loader2,
  Wrench,
  XCircle,
} from "lucide-react"
import type { ToolCall, ToolCallInvocation } from "../types"
import { safeStringify } from "../utils/safeStringify"
import { toolActionLabel, toolStatusLabel, toolStatusPhase } from "../utils/executionCopy"
import { toolDisplayData, type ToolDisplayData } from "../utils/toolSummary"

interface ToolResultCardProps {
  toolCall: ToolCall | ToolCallInvocation | ToolDisplayData
  output?: unknown
  error?: string | null
}

export function ToolResultCard({ toolCall, output, error }: ToolResultCardProps) {
  const [open, setOpen] = useState(false)
  const data = "arguments" in toolCall && "name" in toolCall
    ? { ...toolCall, output: output ?? toolCall.output, error: error ?? toolCall.error }
    : toolDisplayData(toolCall, output, error)
  const argCount = Object.keys(data.arguments ?? {}).length
  const phase = toolStatusPhase(data.status)

  return (
    <div
      className={`w-full overflow-hidden rounded-lg border bg-athena-surface ${
        data.status === "running"
          ? "border-athena-accent/50"
          : data.status === "failed" || data.status === "denied" || data.status === "timeout"
            ? "border-red-500/40"
            : "border-athena-border"
      }`}
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="w-full px-3 py-2 text-left transition-colors hover:bg-athena-bg/60"
      >
        <div className="flex min-w-0 items-center gap-2">
          <StatusIcon status={data.status} />
          <Wrench className="h-4 w-4 flex-shrink-0 text-athena-muted" />
          <span className="min-w-0 flex-1 truncate text-sm font-medium text-athena-text">
            {toolActionLabel(data.name, phase)}
          </span>
          {data.risk_level && (
            <span className={`risk-badge risk-${data.risk_level}`}>
              {riskLabel(data.risk_level)}
            </span>
          )}
          <span className="ml-auto flex flex-shrink-0 items-center gap-1 text-xs text-athena-muted">
            {toolStatusLabel(data.status)}
            {data.duration_ms != null && data.duration_ms > 0 && (
              <span className="flex items-center gap-0.5">
                <Clock className="h-3 w-3" />
                {formatDuration(data.duration_ms)}
              </span>
            )}
            <ChevronDown
              className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`}
            />
          </span>
        </div>
        {data.error && <div className="mt-1 line-clamp-2 text-xs text-athena-danger">操作未能完成，请展开查看原因。</div>}
      </button>

      {open && (
        <div className="space-y-3 border-t border-athena-border px-3 py-2">
          <div className="text-xs text-athena-muted">技术详情 · 工具标识：<code className="break-all">{data.name}</code></div>
          {argCount > 0 && <DetailBlock title={`操作参数（${argCount} 项）`} value={data.arguments} />}
          {data.output !== undefined && data.output !== null && data.output !== "" && (
            <DetailBlock title="原始返回内容" value={data.output} maxLen={50000} />
          )}
          {data.error && (
            <div className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-athena-danger whitespace-pre-wrap">
              {data.error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function StatusIcon({ status }: { status?: ToolCall["status"] }) {
  if (status === "success") return <CheckCircle className="h-4 w-4 flex-shrink-0 text-athena-success" />
  if (status === "failed") return <XCircle className="h-4 w-4 flex-shrink-0 text-athena-danger" />
  if (status === "denied" || status === "timeout") return <AlertTriangle className="h-4 w-4 flex-shrink-0 text-athena-warning" />
  if (status === "paused") return <Clock className="h-4 w-4 flex-shrink-0 text-athena-muted" />
  if (status === "running" || status === "pending") return <Loader2 className="h-4 w-4 flex-shrink-0 animate-spin text-athena-accent" />
  return <Wrench className="h-4 w-4 flex-shrink-0 text-athena-muted" />
}

function riskLabel(risk: NonNullable<ToolCall["risk_level"]>): string {
  if (risk === "high") return "高风险"
  if (risk === "medium") return "中风险"
  return "低风险"
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function DetailBlock({
  title,
  value,
  maxLen = 12000,
}: {
  title: string
  value: unknown
  maxLen?: number
}) {
  return (
    <div>
      <div className="mb-1 text-[11px] text-athena-muted">{title}</div>
      <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-athena-bg px-3 py-2 text-xs text-athena-text/80">
        {safeStringify(value, maxLen)}
      </pre>
    </div>
  )
}
