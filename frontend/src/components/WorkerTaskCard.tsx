import { useMemo, useState } from "react"
import { CheckCircle, ChevronDown, Clock, Loader2, ShieldCheck, XCircle } from "lucide-react"
import type { OrchestrationTask, Step, ToolCall } from "../types"
import { taskDisplayTitle, workerProgressText, workerStatusLabel, workerStatusTone } from "../utils/orchestrationProjection"
import { ToolResultCard } from "./ToolResultCard"

interface WorkerTaskCardProps {
  task: OrchestrationTask
  index: number
  allTasks: OrchestrationTask[]
  steps: Step[]
  toolCalls: ToolCall[]
}

export function WorkerTaskCard({ task, index, allTasks, steps, toolCalls }: WorkerTaskCardProps) {
  const [open, setOpen] = useState(false)
  const taskMap = useMemo(() => new Map(allTasks.map((item) => [item.task_id, item])), [allTasks])
  const relatedSteps = steps.filter((step) => step.run_id === task.worker_run_id || step.parent_run_id === task.worker_run_id || step.parent_run_id === task.parent_run_id)
  const relatedTools = toolCalls.filter((call) => call.run_id === task.worker_run_id || relatedSteps.some((step) => step.id === call.step_id))
  const tone = workerStatusTone(task.status)
  const title = taskDisplayTitle(task, index)

  return (
    <article className={`rounded-lg border bg-athena-bg/35 p-3 ${toneBorder[tone]}`}>
      <button type="button" onClick={() => setOpen((value) => !value)} className="w-full text-left" aria-expanded={open}>
        <div className="flex items-start gap-2">
          <StatusIcon status={task.status} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm font-medium text-athena-text">{title}</span>
              <span className={`text-xs ${toneText[tone]}`}>{workerStatusLabel(task.status)}</span>
              <ChevronDown className={`h-4 w-4 shrink-0 text-athena-muted transition-transform ${open ? "rotate-180" : ""}`} />
            </div>
            <p className="mt-1 break-words text-xs leading-5 text-athena-muted">{workerProgressText(task, taskMap)}</p>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 pl-6 text-[11px] text-athena-muted">
          {task.attempt && task.attempt > 1 && <span>第 {task.attempt} 次尝试</span>}
          {task.duration_ms != null && task.duration_ms > 0 && <span className="inline-flex items-center gap-1"><Clock className="h-3 w-3" />{formatDuration(task.duration_ms)}</span>}
          {task.status === "waiting_approval" && <span className="inline-flex items-center gap-1 text-athena-warning"><ShieldCheck className="h-3 w-3" />等待确认</span>}
        </div>
      </button>

      {open && (
        <div className="mt-3 space-y-3 border-t border-athena-border/70 pt-3">
          {task.result_summary && <div className="rounded-md bg-athena-surface/60 px-3 py-2 text-xs text-athena-text/85">{task.result_summary}</div>}
          {task.error && <div className="rounded-md border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-athena-danger">{task.error}</div>}
          {relatedSteps.length > 0 && <div className="space-y-1 text-xs text-athena-muted"><div className="font-medium text-athena-text/80">执行步骤 · {relatedSteps.length}</div>{relatedSteps.map((step) => <div key={step.id} className="flex items-center gap-2"><span>{step.step_type === "llm_call" ? "整理信息" : "执行工具"}</span><span>{step.status === "completed" ? "已完成" : step.status === "failed" ? "失败" : "处理中"}</span></div>)}</div>}
          {relatedTools.length > 0 && <div className="space-y-2"><div className="text-xs font-medium text-athena-text/80">工具调用</div>{relatedTools.map((tool) => <ToolResultCard key={tool.id} toolCall={tool} />)}</div>}
          <details className="rounded-md bg-athena-bg px-3 py-2"><summary className="cursor-pointer text-xs text-athena-muted">查看技术详情</summary><pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all text-[11px] text-athena-muted">{JSON.stringify({ plan_id: task.plan_id, task_id: task.task_id, worker_run_id: task.worker_run_id, parent_run_id: task.parent_run_id, execution_generation: task.execution_generation, metadata: task.metadata }, null, 2)}</pre></details>
        </div>
      )}
    </article>
  )
}

function StatusIcon({ status }: { status: OrchestrationTask["status"] }) {
  if (status === "completed") return <CheckCircle className="mt-0.5 h-4 w-4 shrink-0 text-athena-success" />
  if (status === "failed" || status === "invalid_output") return <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-athena-danger" />
  if (status === "running") return <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-athena-accent" />
  return <Clock className="mt-0.5 h-4 w-4 shrink-0 text-athena-muted" />
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

const toneText = { muted: "text-athena-muted", accent: "text-athena-accent", warning: "text-athena-warning", success: "text-athena-success", danger: "text-athena-danger" }
const toneBorder = { muted: "border-athena-border", accent: "border-athena-accent/40", warning: "border-yellow-500/40", success: "border-athena-success/30", danger: "border-red-500/35" }
