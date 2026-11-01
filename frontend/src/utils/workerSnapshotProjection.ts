import type {
  ApprovalRequest,
  RunOrchestrationSnapshot,
  WorkerApprovalRequestSnapshot,
  WorkerPlanSnapshot,
  WorkerTaskSnapshot,
} from "../types"

/** 按持久化 sequence 排序；不会把旧 SSE 的临时顺序带入展示。 */
export function orderedWorkerPlans(snapshot: RunOrchestrationSnapshot | null): WorkerPlanSnapshot[] {
  return snapshot ? [...snapshot.worker_plans].sort((left, right) => left.sequence - right.sequence) : []
}

/** 只读取 canonical 快照中属于当前 Run 的计划，避免切换 Run 后串数据。 */
export function plansForRun(snapshot: RunOrchestrationSnapshot | null, runId: string | null): WorkerPlanSnapshot[] {
  if (!snapshot || !runId || snapshot.run.run_id !== runId) return []
  return orderedWorkerPlans(snapshot)
}

export interface WorkerTaskAttemptView {
  task: WorkerTaskSnapshot
  generation: number
  status: string
  capacityReserved: boolean
  terminal: boolean
}

export function taskAttempts(task: WorkerTaskSnapshot): WorkerTaskAttemptView[] {
  return [...task.executions]
    .sort((left, right) => left.generation - right.generation)
    .map((execution) => ({
      task,
      generation: execution.generation,
      status: execution.status,
      capacityReserved: execution.capacity_reserved,
      terminal: ["completed", "succeeded", "failed", "cancelled", "timed_out", "superseded"].includes(execution.status),
    }))
}

const terminalExecutionStatuses = new Set([
  "completed",
  "succeeded",
  "failed",
  "invalid_output",
  "cancelled",
  "timed_out",
  "superseded",
])

/**
 * 远端容量事实的用户文案。运行中的占槽是正常状态，只有终态仍占槽才提示清理。
 * 超时和取消分别表述，避免把超时误说成用户取消。
 */
export function executionCapacityMessage(status: string, capacityReserved: boolean): string | null {
  if (!capacityReserved) return null
  if (!terminalExecutionStatuses.has(status)) return "远端仍占用执行资源"
  if (status === "cancelled") return "取消状态已确定，远端停止清理仍待确认"
  if (status === "timed_out") return "已超时，远端停止清理仍待确认"
  return "终态已确定，远端停止清理仍待确认"
}

export function pendingApprovalItems(snapshot: RunOrchestrationSnapshot | null, runId: string | null) {
  if (!snapshot || !runId || snapshot.run.run_id !== runId) return []
  return snapshot.approvals
    .filter((request) => request.status === "pending")
    .flatMap((request) => request.items
      .filter((item) => item.status === "pending")
      .map((item) => ({ request, item })))
}

/** Canonical item IDs suppress stale legacy SSE approvals after resolution or expiry. */
export function mergePendingApprovals(
  legacy: ApprovalRequest[],
  canonicalRequests: RunOrchestrationSnapshot["approvals"],
  canonicalPending: ApprovalRequest[],
): ApprovalRequest[] {
  const canonicalIds = new Set(canonicalRequests.flatMap((request) => request.items.map((item) => item.approval_id)))
  return [...legacy.filter((approval) => !canonicalIds.has(approval.approval_id)), ...canonicalPending]
}

/** 仅接受当前请求代次的响应，防止切换 Run 后迟到响应覆盖新快照。 */
export function shouldApplySnapshotResponse(
  responseRunId: string,
  activeRunId: string | null,
  responseGeneration: number,
  latestGeneration: number,
): boolean {
  return responseRunId === activeRunId && responseGeneration === latestGeneration
}

/** SSE 回调必须同时属于当前连接且组件尚未卸载。 */
export function shouldHandleStreamCallback(
  disposed: boolean,
  callbackGeneration: number,
  currentGeneration: number,
): boolean {
  return !disposed && callbackGeneration === currentGeneration
}

export function shouldAcceptSnapshotState(existingStatus: string | undefined, incomingStatus: string): boolean {
  const terminal = new Set(["completed", "failed", "cancelled", "budget_exceeded"])
  return !(existingStatus && terminal.has(existingStatus) && !terminal.has(incomingStatus))
}

export function approvalRequestHasExpired(request: WorkerApprovalRequestSnapshot): boolean {
  return request.status === "expired" || request.items.some((item) => item.status === "expired")
}
