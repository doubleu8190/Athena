import type { OrchestrationPlan, OrchestrationTask } from "../types"

export type WorkerStatus = OrchestrationTask["status"]

export interface OrchestrationCounts {
  total: number
  queued: number
  running: number
  waitingApproval: number
  retrying: number
  completed: number
  failed: number
  cancelled: number
}

export interface OrchestrationPlanView {
  plan: OrchestrationPlan
  tasks: OrchestrationTask[]
  counts: OrchestrationCounts
  status: OrchestrationPlan["status"]
}

const STATUS_LABELS: Record<WorkerStatus, string> = {
  queued: "排队中",
  claimed: "已分配",
  running: "处理中",
  waiting_approval: "等待确认",
  retry_wait: "正在重试",
  cancel_requested: "正在取消",
  completed: "已完成",
  failed: "失败",
  timed_out: "已超时",
  invalid_output: "结果待修正",
  cancelled: "已取消",
}

export function workerStatusLabel(status: WorkerStatus): string {
  return STATUS_LABELS[status] || "处理中"
}

export function workerStatusTone(status: WorkerStatus): "muted" | "accent" | "warning" | "success" | "danger" {
  if (status === "completed") return "success"
  if (status === "failed" || status === "timed_out" || status === "invalid_output") return "danger"
  if (status === "waiting_approval" || status === "retry_wait" || status === "cancel_requested") return "warning"
  if (status === "running") return "accent"
  return "muted"
}

export function workerProgressText(task: OrchestrationTask, taskMap: Map<string, OrchestrationTask>): string {
  if (task.blocked_by?.length) {
    const names = task.blocked_by.map((id) => taskMap.get(id)?.title || "上游任务")
    return `等待${names.slice(0, 2).join("、")}${names.length > 2 ? "等任务" : "完成"}`
  }
  if (task.status === "queued" || task.status === "claimed") return "等待可用执行资源"
  if (task.status === "running") return task.objective || "正在处理这项任务"
  if (task.status === "waiting_approval") return "需要确认后才能继续"
  if (task.status === "retry_wait") return `第 ${task.attempt || 2} 次尝试准备中`
  if (task.status === "completed") return task.result_summary || "已完成并返回结果"
  if (task.status === "cancelled" || task.status === "cancel_requested") return "此任务已停止"
  return task.error || "处理未能完成"
}

export function taskDisplayTitle(task: OrchestrationTask, index: number): string {
  const title = task.title.trim()
  return title && !looksInternal(title) ? title : `并行任务 ${index + 1}`
}

export function deriveOrchestrationView(
  plan: OrchestrationPlan | undefined,
  allTasks: OrchestrationTask[],
): OrchestrationPlanView | null {
  if (!plan) return null
  const tasks = allTasks.filter((task) => task.plan_id === plan.plan_id)
  if (tasks.length === 0 && !plan.task_ids?.length) return null
  const counts: OrchestrationCounts = {
    total: tasks.length || plan.task_ids?.length || 0,
    queued: tasks.filter((task) => task.status === "queued" || task.status === "claimed").length,
    running: tasks.filter((task) => task.status === "running").length,
    waitingApproval: tasks.filter((task) => task.status === "waiting_approval").length,
    retrying: tasks.filter((task) => task.status === "retry_wait").length,
    completed: tasks.filter((task) => task.status === "completed").length,
    failed: tasks.filter((task) => task.status === "failed" || task.status === "timed_out" || task.status === "invalid_output").length,
    cancelled: tasks.filter((task) => task.status === "cancelled" || task.status === "cancel_requested").length,
  }
  const status = plan.status || inferPlanStatus(counts)
  return { plan, tasks, counts, status }
}

export function summarizeResult(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim().slice(0, 220)
  if (!value || typeof value !== "object") return null
  const record = value as Record<string, unknown>
  const candidate = record.summary ?? record.result ?? record.message ?? record.output
  return typeof candidate === "string" && candidate.trim() ? candidate.trim().slice(0, 220) : null
}

function inferPlanStatus(counts: OrchestrationCounts): OrchestrationPlan["status"] {
  if (counts.total > 0 && counts.completed === counts.total) return "completed"
  if (counts.failed > 0) return "failed"
  return "running"
}

function looksInternal(title: string): boolean {
  return /^(task|worker|subagent|node|run)[:._\-\d]/i.test(title)
}
