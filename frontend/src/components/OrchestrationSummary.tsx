import { useState } from "react"
import { CheckCircle, ChevronDown, Loader2, Network, XCircle } from "lucide-react"
import type { OrchestrationPlanView, } from "../utils/orchestrationProjection"
import type { Step, ToolCall } from "../types"
import { WorkerTaskCard } from "./WorkerTaskCard"

interface OrchestrationSummaryProps {
  view: OrchestrationPlanView
  steps: Step[]
  toolCalls: ToolCall[]
  defaultExpanded?: boolean
}

export function OrchestrationSummary({ view, steps, toolCalls, defaultExpanded = true }: OrchestrationSummaryProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const { counts } = view
  const active = counts.running + counts.queued + counts.waitingApproval + counts.retrying > 0
  return <section className="my-4 rounded-lg border border-athena-border bg-athena-surface/45 p-3" aria-label="并行任务">
    <button type="button" onClick={() => setExpanded((value) => !value)} className="flex w-full items-center gap-2 text-left">
      {active ? <Loader2 className="h-4 w-4 animate-spin text-athena-accent" /> : view.status === "failed" ? <XCircle className="h-4 w-4 text-athena-danger" /> : <CheckCircle className="h-4 w-4 text-athena-success" />}
      <span className="min-w-0 flex-1 text-sm font-semibold text-athena-text">{active ? `正在并行处理 ${counts.total} 项任务` : view.status === "failed" ? "并行任务未全部完成" : "并行任务已完成"}</span>
      <span className="inline-flex items-center gap-1 text-xs text-athena-muted"><Network className="h-3.5 w-3.5" />{counts.completed}/{counts.total} 已完成</span>
      <ChevronDown className={`h-4 w-4 text-athena-muted transition-transform ${expanded ? "rotate-180" : ""}`} />
    </button>
    {expanded && <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">{view.tasks.map((task, index) => <WorkerTaskCard key={`${task.plan_id}:${task.task_id}`} task={task} index={index} allTasks={view.tasks} steps={steps} toolCalls={toolCalls} />)}</div>}
    {view.plan.synthesis_status === "running" && <div className="mt-3 flex items-center gap-2 text-xs text-athena-accent"><Loader2 className="h-3.5 w-3.5 animate-spin" />正在汇总处理结果</div>}
    {view.plan.synthesis_status === "completed" && <div className="mt-3 text-xs text-athena-success">处理结果已汇总</div>}
  </section>
}
