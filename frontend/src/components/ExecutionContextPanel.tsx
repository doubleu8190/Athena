import { useMemo, useState } from "react"
import {
  CheckCircle2,
  ChevronDown,
  Clock3,
  FileText,
  ListChecks,
  Loader2,
  ShieldAlert,
  X,
  XCircle,
} from "lucide-react"
import type {
  AgentStatus,
  Attachment,
  ConnectionStatus,
  ExecutionTimelineEntry,
  Message,
  OrchestrationPlan,
  OrchestrationTask,
} from "../types"
import { deriveOrchestrationView } from "../utils/orchestrationProjection"

interface ExecutionContextPanelProps {
  id?: string
  messages: Message[]
  agentStatus: AgentStatus
  connectionStatus: ConnectionStatus
  executionTimeline: ExecutionTimelineEntry[]
  orchestrationPlans: OrchestrationPlan[]
  orchestrationTasks: OrchestrationTask[]
  attachments: Attachment[]
  onClose: () => void
}

function statusLabel(status: OrchestrationTask["status"]): string {
  switch (status) {
    case "completed": return "已完成"
    case "running": return "处理中"
    case "waiting_approval": return "待确认"
    case "retry_wait": return "准备重试"
    case "failed":
    case "timed_out":
    case "invalid_output": return "失败"
    case "cancelled":
    case "cancel_requested": return "已取消"
    default: return "排队中"
  }
}

function taskTone(status: OrchestrationTask["status"]): string {
  if (status === "completed") return "text-athena-success"
  if (["failed", "timed_out", "invalid_output"].includes(status)) return "text-athena-danger"
  if (status === "waiting_approval") return "text-athena-warning"
  if (["running", "retry_wait"].includes(status)) return "text-athena-accent"
  return "text-athena-muted"
}

function TaskIcon({ status }: { status: OrchestrationTask["status"] }) {
  if (status === "completed") return <CheckCircle2 className="h-4 w-4 text-athena-success" />
  if (["failed", "timed_out", "invalid_output"].includes(status)) return <XCircle className="h-4 w-4 text-athena-danger" />
  if (status === "waiting_approval") return <ShieldAlert className="h-4 w-4 text-athena-warning" />
  if (["running", "retry_wait"].includes(status)) return <Loader2 className="h-4 w-4 animate-spin text-athena-accent" />
  return <Clock3 className="h-4 w-4 text-athena-muted" />
}

function timelineIcon(entry: ExecutionTimelineEntry) {
  if (entry.event_type.startsWith("approval.")) return <ShieldAlert className="h-3.5 w-3.5 text-athena-warning" />
  if (entry.status === "completed") return <CheckCircle2 className="h-3.5 w-3.5 text-athena-success" />
  if (entry.status === "failed") return <XCircle className="h-3.5 w-3.5 text-athena-danger" />
  if (entry.status === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin text-athena-accent" />
  return <Clock3 className="h-3.5 w-3.5 text-athena-muted" />
}

function formatTime(timestamp: string): string {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return ""
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
}

function currentRunId(entries: ExecutionTimelineEntry[], messages: Message[]): string | null {
  const lifecycleRun = [...entries].reverse().find((entry) => entry.event_type.startsWith("run.") && entry.run_id)?.run_id
  if (lifecycleRun) return lifecycleRun
  const messageRun = [...messages].reverse().find((message) => message.run_id)?.run_id
  if (messageRun) return messageRun
  return [...entries].reverse().find((entry) => entry.run_id)?.run_id ?? null
}

export function ExecutionContextPanel({
  id,
  messages,
  agentStatus,
  connectionStatus,
  executionTimeline,
  orchestrationPlans,
  orchestrationTasks,
  attachments,
  onClose,
}: ExecutionContextPanelProps) {
  const [showAllEvents, setShowAllEvents] = useState(false)
  const runId = currentRunId(executionTimeline, messages)
  const plan = [...orchestrationPlans].reverse().find((item) => !runId || item.run_id === runId)
  const planTasks = orchestrationTasks.filter((task) => plan ? task.plan_id === plan.plan_id : !runId || task.run_id === runId)
  const view = useMemo(
    () => deriveOrchestrationView(plan, planTasks),
    [plan, planTasks],
  )
  const timeline = useMemo(() => {
    const filtered = executionTimeline.filter((entry) => !runId || entry.run_id === runId)
    return [...filtered].sort((a, b) => {
      if (filtered.every((entry) => entry.session_seq != null)) return (a.session_seq ?? 0) - (b.session_seq ?? 0)
      const timeDelta = new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      return timeDelta || (a.session_seq ?? 0) - (b.session_seq ?? 0)
    })
  }, [executionTimeline, runId])
  const active = agentStatus === "thinking" || agentStatus === "running" || agentStatus === "waiting_approval"
  const stateLabel = agentStatus === "waiting_approval" ? "等待确认" : agentStatus === "running" || agentStatus === "thinking" ? "执行中" : agentStatus === "error" ? "执行失败" : agentStatus === "paused" ? "已暂停" : "空闲"
  const connectionLabel = connectionStatus === "connected" ? "事件流已连接" : connectionStatus === "connecting" ? "事件流连接中" : connectionStatus === "error" ? "事件流异常" : "事件流已断开"

  return (
    <aside id={id} className="execution-context-panel flex h-full w-[min(92vw,360px)] shrink-0 flex-col border-l border-athena-border bg-athena-surface shadow-2xl xl:relative xl:z-auto xl:w-[320px] xl:bg-athena-surface/70 xl:shadow-none" aria-label="执行上下文">
      <div className="flex items-center gap-2 border-b border-athena-border px-4 py-3">
        <ListChecks className="h-4 w-4 text-athena-accent" />
        <span className="text-sm font-semibold text-athena-text">执行上下文</span>
        <span className={`ml-auto text-xs ${active ? "text-athena-accent" : "text-athena-muted"}`}>{stateLabel}</span>
        <button type="button" onClick={onClose} className="ml-1 inline-flex h-7 w-7 items-center justify-center rounded-md text-athena-muted hover:bg-athena-bg hover:text-athena-text" title="关闭执行上下文" aria-label="关闭执行上下文">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <section className="context-panel-card rounded-lg border border-athena-border bg-athena-bg/40 p-3">
          <div className="flex items-center justify-between gap-2">
            <div>
              <div className="text-xs font-semibold text-athena-text">当前请求</div>
              <div className="mt-1 font-mono text-[10px] text-athena-muted">{runId ?? "尚未创建运行"}</div>
            </div>
            {active && <Loader2 className="h-4 w-4 animate-spin text-athena-accent" />}
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3 border-t border-athena-border/70 pt-3">
            <div><div className="text-[10px] text-athena-muted">任务</div><div className="mt-1 text-xs text-athena-text">{view ? `${view.counts.completed} / ${view.counts.total} 完成` : "无并行任务"}</div></div>
            <div><div className="text-[10px] text-athena-muted">附件</div><div className="mt-1 flex items-center gap-1 text-xs text-athena-text"><FileText className="h-3.5 w-3.5 text-athena-muted" />{attachments.length}</div></div>
          </div>
        </section>

        <section className="context-panel-card rounded-lg border border-athena-border bg-athena-bg/40 p-3">
          <div className="flex items-center justify-between"><div className="text-xs font-semibold text-athena-text">任务进度</div>{view && <span className="font-mono text-[10px] text-athena-muted">{view.counts.completed}/{view.counts.total}</span>}</div>
          {!view ? <p className="mt-3 text-xs text-athena-muted">当前请求没有并行任务。</p> : <div className="mt-2 divide-y divide-athena-border/60">{view.tasks.map((task) => <div key={`${task.plan_id}:${task.task_id}`} className="flex items-start gap-2 py-2 first:pt-1 last:pb-0"><TaskIcon status={task.status} /><div className="min-w-0 flex-1"><div className="truncate text-xs text-athena-text">{task.title || "未命名任务"}</div><div className={`mt-1 text-[10px] ${taskTone(task.status)}`}>{statusLabel(task.status)}{task.attempt && task.attempt > 1 ? ` · 第 ${task.attempt} 次尝试` : ""}</div></div></div>)}</div>}
        </section>

        <section className="context-panel-card rounded-lg border border-athena-border bg-athena-bg/40 p-3">
          <div className="flex items-center justify-between"><div className="text-xs font-semibold text-athena-text">操作记录</div><div className="flex items-center gap-2"><span className="font-mono text-[10px] text-athena-muted">{timeline.length} 条</span>{timeline.length > 5 && <button type="button" onClick={() => setShowAllEvents((value) => !value)} className="text-[10px] text-athena-accent hover:underline" aria-expanded={showAllEvents}>{showAllEvents ? "收起" : "全部"}</button>}</div></div>
          {timeline.length === 0 ? <p className="mt-3 text-xs text-athena-muted">还没有可展示的操作记录。</p> : <div className="mt-2 divide-y divide-athena-border/60">{(showAllEvents ? timeline : timeline.slice(-5)).reverse().map((entry) => <details key={entry.id} className="group py-2 first:pt-1 last:pb-0"><summary className="flex cursor-pointer list-none items-start gap-2"><span className="mt-0.5">{timelineIcon(entry)}</span><span className="min-w-0 flex-1"><span className="block truncate text-xs text-athena-text">{entry.label}</span><span className="mt-1 block text-[10px] text-athena-muted">{formatTime(entry.timestamp)} · {entry.status}</span></span><ChevronDown className="mt-0.5 h-3.5 w-3.5 text-athena-muted transition-transform group-open:rotate-180" /></summary>{(entry.detail || entry.metadata) && <div className="ml-5 mt-2 rounded-md bg-athena-bg px-2.5 py-2 text-[10px] leading-4 text-athena-muted">{entry.detail}{entry.metadata && <pre className="mt-1 max-h-28 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(entry.metadata, null, 2)}</pre>}</div>}</details>)}</div>}
        </section>
      </div>
      <div className="flex items-center justify-between border-t border-athena-border px-4 py-2 text-[10px] text-athena-muted"><span>{connectionLabel}</span><span className="font-mono">{timeline.at(-1)?.session_seq != null ? `SEQ ${timeline.at(-1)?.session_seq}` : "实时"}</span></div>
    </aside>
  )
}

