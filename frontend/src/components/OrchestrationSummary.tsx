import { useState } from "react"
import { CheckCircle, ChevronDown, Loader2, XCircle } from "lucide-react"
import type { WorkerPlanSnapshot } from "../types"
import { WorkerTaskCard } from "./WorkerTaskCard"

export function OrchestrationSummary({ plan }: { plan: WorkerPlanSnapshot }) {
  const [expanded, setExpanded] = useState(true)
  const active = !["completed", "failed", "cancelled"].includes(plan.status)
  const title = plan.status === "completed" ? "本轮任务已完成" : plan.status === "failed" ? "本轮任务失败" : plan.status === "cancelled" ? "本轮任务已取消" : `第 ${plan.sequence} 轮任务执行中`
  return <section className="my-3 rounded-lg border border-athena-border bg-athena-surface/45 p-3" aria-label={`第 ${plan.sequence} 轮任务`}>
    <button type="button" onClick={() => setExpanded((value) => !value)} className="flex w-full items-center gap-2 text-left">
      {active ? <Loader2 className="h-4 w-4 animate-spin text-athena-accent" /> : plan.status === "failed" ? <XCircle className="h-4 w-4 text-athena-danger" /> : <CheckCircle className="h-4 w-4 text-athena-success" />}
      <span className="min-w-0 flex-1 text-sm font-semibold text-athena-text">{title}</span>
      <span className="text-xs text-athena-muted">{plan.terminal_task_count}/{plan.expected_task_count} 已结束</span>
      <ChevronDown className={`h-4 w-4 text-athena-muted transition-transform ${expanded ? "rotate-180" : ""}`} />
    </button>
    {expanded && <div className="mt-3 space-y-2">{plan.tasks.map((task, index) => <WorkerTaskCard key={task.task_id} task={task} index={index} />)}</div>}
  </section>
}
