import assert from "node:assert/strict"
import test from "node:test"
import {
  approvalRequestHasExpired,
  executionCapacityMessage,
  mergePendingApprovals,
  orderedWorkerPlans,
  pendingApprovalItems,
  plansForRun,
  shouldApplySnapshotResponse,
  shouldAcceptSnapshotState,
  shouldHandleStreamCallback,
  taskAttempts,
} from "../src/utils/workerSnapshotProjection.ts"
import { useChatStore } from "../src/store/chatStore.ts"
import type { RunOrchestrationSnapshot } from "../src/types/index.ts"

const snapshot: RunOrchestrationSnapshot = {
  run: {
    run_id: "run-1", session_id: "session-1", status: "running", root_thread_id: "root-1",
    cancellation_epoch: 0, cancellation_external_effect_status: "none",
    created_at: "2026-10-31T00:00:00Z", updated_at: "2026-10-31T00:00:02Z",
  },
  worker_plans: [
    {
      plan_id: "plan-2", run_id: "run-1", sequence: 2, goal: "复核", status: "running",
      expected_task_count: 1, terminal_task_count: 0, root_wait_generation: 0,
      created_at: "2026-10-31T00:00:02Z", completed_at: null,
      tasks: [{
        task_id: "task-retry", operation_id: "op-1", task_ref: "review", objective: "复核结果",
        acceptance_criteria: "无矛盾", status: "running", active_execution_id: "exec-2", generation: 2,
        output: null, error: null,
        executions: [
          { execution_id: "exec-1", generation: 1, status: "timed_out", started_at: "2026-10-31T00:00:03Z", deadline_at: "2026-10-31T00:00:04Z", completed_at: "2026-10-31T00:00:04Z", cancellation_epoch: 0, capacity_reserved: false, error: { code: "timeout" } },
          { execution_id: "exec-2", generation: 2, status: "running", started_at: "2026-10-31T00:00:05Z", deadline_at: "2026-10-31T00:00:09Z", completed_at: null, cancellation_epoch: 0, capacity_reserved: true, error: null },
        ],
      }],
    },
    {
      plan_id: "plan-1", run_id: "run-1", sequence: 1, goal: "收集", status: "completed",
      expected_task_count: 1, terminal_task_count: 1, root_wait_generation: 1,
      created_at: "2026-10-31T00:00:00Z", completed_at: "2026-10-31T00:00:02Z", tasks: [],
    },
  ],
  approvals: [{
    approval_request_id: "req-1", pause_id: "pause-1", approval_batch_id: "batch-1", run_id: "run-1",
    plan_id: "plan-2", task_id: "task-retry", execution_id: "exec-2", generation: 2,
    status: "pending", expires_at: "2026-10-31T00:01:00Z", received_at: "2026-10-31T00:00:06Z",
    items: [{
      item_id: "item-1", approval_id: "approval-1", tool_name: "write", summary: "写入结果",
      risk_level: "high", arguments: { path: "report.txt" }, arguments_sha256: "a".repeat(64),
      status: "pending", decision: null, created_at: "2026-10-31T00:00:06Z", decided_at: null,
    }],
  }],
}

test("multi-round plans are ordered by sequence and scoped to the selected run", () => {
  assert.deepEqual(orderedWorkerPlans(snapshot).map((plan) => plan.sequence), [1, 2])
  assert.deepEqual(plansForRun(snapshot, "run-1").map((plan) => plan.plan_id), ["plan-1", "plan-2"])
  assert.deepEqual(plansForRun(snapshot, "run-2"), [])
})

test("execution generations remain ordered and preserve timeout/capacity facts", () => {
  const task = snapshot.worker_plans[0].tasks[0]
  const attempts = taskAttempts(task)
  assert.deepEqual(attempts.map((attempt) => attempt.generation), [1, 2])
  assert.equal(attempts[0].terminal, true)
  assert.equal(attempts[0].capacityReserved, false)
  assert.equal(attempts[1].terminal, false)
  assert.equal(attempts[1].capacityReserved, true)
})

test("capacity copy distinguishes active execution from timeout and cancellation cleanup", () => {
  assert.equal(executionCapacityMessage("running", true), "远端仍占用执行资源")
  assert.equal(executionCapacityMessage("waiting_approval", true), "远端仍占用执行资源")
  assert.equal(executionCapacityMessage("timed_out", true), "已超时，远端停止清理仍待确认")
  assert.equal(executionCapacityMessage("cancelled", true), "取消状态已确定，远端停止清理仍待确认")
  assert.equal(executionCapacityMessage("completed", false), null)
})

test("approval items are pending only for the matching run and expiry is server-derived", () => {
  assert.equal(pendingApprovalItems(snapshot, "run-1").length, 1)
  assert.equal(pendingApprovalItems(snapshot, "run-2").length, 0)
  assert.equal(approvalRequestHasExpired(snapshot.approvals[0]), false)
  const expired = { ...snapshot.approvals[0], status: "expired" }
  assert.equal(approvalRequestHasExpired(expired), true)
  const legacyPending = [
    { approval_id: "approval-1", tool_name: "write", arguments: {}, risk_level: "high" as const },
    { approval_id: "legacy-only", tool_name: "read", arguments: {}, risk_level: "low" as const },
  ]
  assert.deepEqual(mergePendingApprovals(legacyPending, snapshot.approvals, []).map((item) => item.approval_id), ["legacy-only"])
  const resolvedCanonical = [{ ...snapshot.approvals[0], status: "resolved" as const, items: [{ ...snapshot.approvals[0].items[0], status: "resolved" as const, decision: "approved" }] }]
  assert.deepEqual(mergePendingApprovals(legacyPending, resolvedCanonical, []).map((item) => item.approval_id), ["legacy-only"])
  const expiredCanonical = [{ ...snapshot.approvals[0], status: "expired" as const, items: [{ ...snapshot.approvals[0].items[0], status: "expired" as const }] }]
  assert.deepEqual(mergePendingApprovals(legacyPending, expiredCanonical, []).map((item) => item.approval_id), ["legacy-only"])
})

test("late snapshot response cannot overwrite a switched run or newer request", () => {
  assert.equal(shouldApplySnapshotResponse("run-1", "run-1", 3, 3), true)
  assert.equal(shouldApplySnapshotResponse("run-1", "run-2", 3, 3), false)
  assert.equal(shouldApplySnapshotResponse("run-1", "run-1", 2, 3), false)
})

test("SSE open and error callbacks are ignored after cleanup or reconnect", () => {
  assert.equal(shouldHandleStreamCallback(false, 0, 0), true)
  assert.equal(shouldHandleStreamCallback(true, 0, 0), false)
  assert.equal(shouldHandleStreamCallback(true, 1, 1), false)
  assert.equal(shouldHandleStreamCallback(false, 0, 1), false)
  assert.equal(shouldHandleStreamCallback(false, 1, 1), true)
})

test("terminal run facts cannot be regressed by a late non-terminal snapshot", () => {
  assert.equal(shouldAcceptSnapshotState("completed", "running"), false)
  assert.equal(shouldAcceptSnapshotState("cancelled", "queued"), false)
  assert.equal(shouldAcceptSnapshotState("running", "completed"), true)
  assert.equal(shouldAcceptSnapshotState(undefined, "running"), true)
})

test("snapshot request sequence is never reused across A to B to A", () => {
  const store = useChatStore.getState()
  store.clearOrchestrationSnapshots()
  const firstA = store.beginOrchestrationSnapshotRequest("run-a")
  const runASnapshot = { ...snapshot, run: { ...snapshot.run, run_id: "run-a" } }
  store.setOrchestrationSnapshot(runASnapshot, firstA)
  assert.equal(useChatStore.getState().orchestrationSnapshots["run-a"]?.run.run_id, "run-a")
  store.clearOrchestrationSnapshots()
  const firstB = store.beginOrchestrationSnapshotRequest("run-b")
  store.clearOrchestrationSnapshots()
  const secondA = store.beginOrchestrationSnapshotRequest("run-a")
  assert.ok(firstA < firstB)
  assert.ok(firstB < secondA)
  assert.equal(useChatStore.getState().orchestrationSnapshotGenerations["run-a"], secondA)
  store.setOrchestrationSnapshot(runASnapshot, firstA)
  assert.equal(useChatStore.getState().orchestrationSnapshots["run-a"], undefined)
  store.setOrchestrationSnapshot(runASnapshot, secondA)
  assert.equal(useChatStore.getState().orchestrationSnapshots["run-a"]?.run.run_id, "run-a")
})
