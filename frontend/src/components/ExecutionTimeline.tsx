import { useEffect, useState } from "react"
import {
  Bot,
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Clock,
  FileText,
  Loader2,
  MessageSquareText,
  ShieldCheck,
  Wrench,
  XCircle,
} from "lucide-react"
import type { ExecutionTimelineEntry } from "../types"
import { localizedStatus } from "../utils/executionCopy"

interface ExecutionTimelineProps {
  entries: ExecutionTimelineEntry[]
  defaultExpanded?: boolean
  compact?: boolean
}

export function ExecutionTimeline({ entries, defaultExpanded = false, compact = false }: ExecutionTimelineProps) {
  const [isOpen, setIsOpen] = useState(defaultExpanded)

  // 当前请求完成后自动收起；历史请求初始保持收起状态。
  useEffect(() => {
    setIsOpen(defaultExpanded)
  }, [defaultExpanded])

  if (entries.length === 0) return null

  if (compact) {
    return <div className="processing-group py-1">{entries.map((entry) => <TimelineEntryRow key={entry.id} entry={entry} compact />)}</div>
  }

  return (
    <section className="my-5" aria-label="操作记录">
      <button
        type="button"
        onClick={() => setIsOpen((value) => !value)}
        aria-expanded={isOpen}
        className="phase-divider w-full cursor-pointer"
      >
        <span className="phase-label phase-processing">
          {isOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          操作记录
        </span>
        <span className="text-xs text-athena-muted">
          {entries.length} 条记录
        </span>
      </button>
      {isOpen && <div className="processing-group py-2">
        {entries.map((entry) => <TimelineEntryRow key={entry.id} entry={entry} />)}
      </div>}
    </section>
  )
}

function TimelineEntryRow({ entry, compact = false }: { entry: ExecutionTimelineEntry; compact?: boolean }) {
  const [isExpanded, setIsExpanded] = useState(false)
  const hasDetails = Boolean(entry.detail || entry.metadata)
  return (
    <div className={`timeline-node thinking-node ${compact ? "my-2" : ""}`}>
      <div className="flex gap-3">
        {!compact && (
          <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-athena-border bg-athena-surface">
            <EntryIcon entry={entry} />
          </div>
        )}
        <div className="min-w-0 flex-1 border-b border-athena-border/60 pb-3">
          <button
            type="button"
            disabled={!hasDetails}
            onClick={() => setIsExpanded((value) => !value)}
            className="flex w-full items-start gap-2 text-left disabled:cursor-default"
          >
            {compact && <EntryIcon entry={entry} />}
            <span className="min-w-0 flex-1 text-sm font-medium text-athena-text">{entry.label}</span>
            <StatusLabel status={entry.status} />
            {hasDetails && (
              <ChevronDown className={`mt-0.5 h-4 w-4 shrink-0 text-athena-muted transition-transform ${isExpanded ? "rotate-180" : ""}`} />
            )}
          </button>
          <div className="mt-1 flex items-center gap-1 text-xs text-athena-muted">
            <Clock className="h-3 w-3" />
            {formatTime(entry.timestamp)}
          </div>
          {isExpanded && entry.detail && (
            <div className="mt-2 whitespace-pre-wrap break-words rounded-md bg-athena-bg px-3 py-2 text-xs text-athena-text/80">
              {entry.detail}
            </div>
          )}
          {isExpanded && entry.metadata && Object.keys(entry.metadata).length > 0 && (
            <details className="mt-2 rounded-md bg-athena-bg px-3 py-2">
              <summary className="cursor-pointer text-xs text-athena-muted">查看技术详情</summary>
              <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all text-xs text-athena-muted">
                {JSON.stringify(entry.metadata, null, 2)}
              </pre>
            </details>
          )}
        </div>
      </div>
    </div>
  )
}

function EntryIcon({ entry }: { entry: ExecutionTimelineEntry }) {
  if (entry.status === "running") return <Loader2 className="h-4 w-4 animate-spin text-athena-accent" />
  if (entry.status === "failed") return <XCircle className="h-4 w-4 text-athena-danger" />
  if (entry.status === "cancelled") return <XCircle className="h-4 w-4 text-athena-muted" />
  if (entry.status === "completed") return <CheckCircle className="h-4 w-4 text-athena-success" />
  if (entry.status === "paused") return <Clock className="h-4 w-4 text-athena-muted" />
  if (entry.event_type.startsWith("tool.")) return <Wrench className="h-4 w-4 text-yellow-400" />
  if (entry.event_type.startsWith("approval.")) return <ShieldCheck className="h-4 w-4 text-athena-warning" />
  if (entry.event_type.includes("file") || entry.event_type.includes("attachment")) return <FileText className="h-4 w-4 text-athena-accent" />
  if (entry.event_type.startsWith("subagent") || entry.event_type.startsWith("task.")) return <Bot className="h-4 w-4 text-athena-accent" />
  return <MessageSquareText className="h-4 w-4 text-athena-muted" />
}

function StatusLabel({ status }: { status: ExecutionTimelineEntry["status"] }) {
  const label = localizedStatus(status)
  const color = status === "failed" ? "text-athena-danger" : status === "completed" ? "text-athena-success" : status === "running" ? "text-athena-accent" : "text-athena-muted"
  return <span className={`shrink-0 text-xs ${color}`}>{label}</span>
}

function formatTime(timestamp: string): string {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return ""
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}
