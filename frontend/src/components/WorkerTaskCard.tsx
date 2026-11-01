import { useState } from "react"
import { CheckCircle, ChevronDown, Clock, Loader2, ShieldCheck, XCircle } from "lucide-react"
import type { JsonValue, WorkerTaskSnapshot } from "../types"
import { executionCapacityMessage } from "../utils/workerSnapshotProjection"

interface WorkerTaskCardProps {
  task: WorkerTaskSnapshot
  index: number
}

export function WorkerTaskCard({ task, index }: WorkerTaskCardProps) {
  const [open, setOpen] = useState(false)
  const tone = taskTone(task.status)
  return (
    <article className={`rounded-lg border bg-athena-bg/35 p-3 ${tone.border}`}>
      <button type="button" onClick={() => setOpen((value) => !value)} className="w-full text-left" aria-expanded={open}>
        <div className="flex items-start gap-2">
          <StatusIcon status={task.status} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-athena-text">{task.task_ref || `任务 ${index + 1}`}</span>
              <span className={`text-xs ${tone.text}`}>{taskStatusLabel(task.status)}</span>
              <ChevronDown className={`h-4 w-4 shrink-0 text-athena-muted transition-transform ${open ? "rotate-180" : ""}`} />
            </div>
            <p className="mt-1 break-words text-xs leading-5 text-athena-muted">{task.objective || "未提供任务说明"}</p>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 pl-6 text-[11px] text-athena-muted">
          <span>第 {task.generation} 代</span>
          <span>{task.executions.length} 次执行</span>
          {task.status === "waiting_approval" && <span className="inline-flex items-center gap-1 text-athena-warning"><ShieldCheck className="h-3 w-3" />等待确认</span>}
        </div>
      </button>
      {open && <div className="mt-3 space-y-3 border-t border-athena-border/70 pt-3">
        {task.acceptance_criteria && <InfoBlock title="验收标准" value={task.acceptance_criteria} />}
        {task.output != null && <InfoBlock title="输出" value={formatJson(task.output)} />}
        {task.error != null && <InfoBlock title="错误" value={formatJson(task.error)} danger />}
        <div>
          <div className="mb-1.5 text-xs font-medium text-athena-text/80">执行尝试</div>
          <div className="space-y-2">
            {task.executions.map((execution) => <div key={execution.execution_id} className="rounded-md border border-athena-border bg-athena-bg px-3 py-2 text-xs">
              <div className="flex items-center gap-2"><StatusIcon status={execution.status} small /><span className="font-medium">第 {execution.generation} 次</span><span className="text-athena-muted">{executionStatusLabel(execution.status)}</span>{executionCapacityMessage(execution.status, execution.capacity_reserved) && <span className="ml-auto text-[10px] text-athena-warning">{executionCapacityMessage(execution.status, execution.capacity_reserved)}</span>}</div>
              <div className="mt-1 text-[10px] text-athena-muted">{execution.started_at ? `开始 ${formatTime(execution.started_at)}` : "尚未开始"}{execution.completed_at ? ` · 结束 ${formatTime(execution.completed_at)}` : ""}{execution.deadline_at ? ` · 截止 ${formatTime(execution.deadline_at)}` : ""}</div>
              {execution.error != null && <pre className="mt-2 max-h-28 overflow-auto whitespace-pre-wrap break-words text-[10px] text-athena-danger">{formatJson(execution.error)}</pre>}
            </div>)}
          </div>
        </div>
        <details className="rounded-md bg-athena-bg px-3 py-2"><summary className="cursor-pointer text-xs text-athena-muted">查看任务标识</summary><div className="mt-2 space-y-1 text-[10px] text-athena-muted"><div>task_ref: {task.task_ref}</div><div>operation_id: {task.operation_id}</div></div></details>
      </div>}
    </article>
  )
}

function InfoBlock({ title, value, danger = false }: { title: string; value: string; danger?: boolean }) {
  return <div className={`rounded-md border px-3 py-2 text-xs ${danger ? "border-red-500/25 bg-red-500/10 text-athena-danger" : "bg-athena-surface/60 text-athena-text/85"}`}><div className="mb-1 text-[10px] text-athena-muted">{title}</div><pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words">{value}</pre></div>
}

function StatusIcon({ status, small = false }: { status: string; small?: boolean }) {
  const className = `${small ? "h-3 w-3" : "mt-0.5 h-4 w-4"} shrink-0`
  if (["completed", "succeeded"].includes(status)) return <CheckCircle className={`${className} text-athena-success`} />
  if (["failed", "invalid_output", "timed_out"].includes(status)) return <XCircle className={`${className} text-athena-danger`} />
  if (["running", "dispatching"].includes(status)) return <Loader2 className={`${className} animate-spin text-athena-accent`} />
  if (status === "waiting_approval") return <ShieldCheck className={`${className} text-athena-warning`} />
  return <Clock className={`${className} text-athena-muted`} />
}

function taskStatusLabel(value: string): string {
  return ({ queued: "排队中", claimed: "已分配", running: "执行中", waiting_approval: "等待审批", retry_wait: "等待重试", completed: "已完成", failed: "失败", invalid_output: "输出无效", cancelled: "已取消" } as Record<string, string>)[value] || value
}

function executionStatusLabel(value: string): string {
  return ({ queued: "排队中", dispatching: "发送中", running: "执行中", waiting_approval: "等待审批", retry_wait: "等待重试", succeeded: "已完成", completed: "已完成", failed: "失败", timed_out: "已超时", cancelled: "已取消", superseded: "已替代" } as Record<string, string>)[value] || value
}

function taskTone(status: string): { text: string; border: string } {
  if (status === "completed") return { text: "text-athena-success", border: "border-athena-success/30" }
  if (["failed", "invalid_output"].includes(status)) return { text: "text-athena-danger", border: "border-red-500/35" }
  if (["waiting_approval", "retry_wait"].includes(status)) return { text: "text-athena-warning", border: "border-yellow-500/40" }
  if (status === "running") return { text: "text-athena-accent", border: "border-athena-accent/40" }
  return { text: "text-athena-muted", border: "border-athena-border" }
}

function formatJson(value: JsonValue): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2)
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false })
}
