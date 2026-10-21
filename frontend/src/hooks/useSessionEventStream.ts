import { useEffect, useRef } from "react"
import { useChatStore } from "../store/chatStore"
import { apiClient } from "../api/client"
import type { ApplicationEventEnvelope, ExecutionError } from "../types/events"
import type { ExecutionTimelineEntry, Message, ToolCall, OrchestrationPlan, OrchestrationTask, RunTimelineEvent } from "../types"
import { approvalActionDescription, toolActionLabel } from "../utils/executionCopy"
import { summarizeResult } from "../utils/orchestrationProjection"

interface StreamProjection {
  nextChunkId: number
  finalChunkId: number | null
  content: string
  status: "created" | "streaming" | "completed" | "failed"
  pending: Map<number, string>
  snapshotVersion: number
}

function applyPendingChunks(stream: StreamProjection) {
  while (stream.pending.has(stream.nextChunkId)) {
    stream.content += stream.pending.get(stream.nextChunkId) || ""
    stream.pending.delete(stream.nextChunkId)
    stream.nextChunkId += 1
  }
}

/**
 * 订阅会话级有序 SSE。session_seq 负责事件去重，stream_id/chunk_id 负责内容组装。
 * Snapshot 只修复流状态，不覆盖已经按 Chunk 组装出的更新版本。
 */
export function useSessionEventStream(sessionId: string | null, apiBase: string) {
  const sourceRef = useRef<EventSource | null>(null)
  const lastSeqRef = useRef(0)
  const streamsRef = useRef(new Map<string, StreamProjection>())
  const {
    setConnectionStatus, setAgentStatus, addMessage, updateMessage, setAttachments,
    addStep, updateStep, addToolCall, updateToolCall, addApproval, mergeApprovals, resolveApproval,
    updateSession,
    clearSteps, clearToolCalls, clearApprovals, clearThinking, setThinking, setError, setErrorDetail, clearErrorDetail,
    upsertOrchestrationTask, clearOrchestrationTasks, upsertOrchestrationPlan, clearOrchestrationPlans,
    upsertExecutionTimelineEntry, clearExecutionTimeline,
    appendRunTimelineEvent, clearRunTimelineEvents,
  } = useChatStore()

  useEffect(() => {
    sourceRef.current?.close()
    sourceRef.current = null
    lastSeqRef.current = 0
    streamsRef.current.clear()
    clearSteps()
    clearToolCalls()
    clearApprovals()
    clearThinking()
    clearErrorDetail()
    clearOrchestrationTasks()
    clearOrchestrationPlans()
    clearExecutionTimeline()
    clearRunTimelineEvents()
    if (!sessionId) {
      setConnectionStatus("disconnected")
      return
    }

    const url = `${apiBase.replace(/\/$/, "")}/api/sessions/${encodeURIComponent(sessionId)}/events`
    const source = new EventSource(url, { withCredentials: true })
    sourceRef.current = source
    setConnectionStatus("connecting")
    const syncPendingApprovals = () => {
      void apiClient.listPendingApprovals(sessionId)
        .then((approvals) => {
          if (useChatStore.getState().activeSessionId !== sessionId) return
          const pending = approvals.map((approval) => ({
            ...approval,
            session_id: approval.session_id || sessionId,
          }))
          mergeApprovals(pending)
          if (pending.length > 0) setAgentStatus("waiting_approval")
        })
        .catch(() => {
          // API 暂时不可用时保留已有事件状态，等待下一次重连同步。
        })
    }
    source.onopen = () => {
      setConnectionStatus("connected")
      syncPendingApprovals()
    }
    source.onerror = () => setConnectionStatus("error")
    syncPendingApprovals()

    const upsertAssistant = (runId: string, content: string, streamId?: string) => {
      if (!runId) return
      const state = useChatStore.getState()
      const existing = state.messages.find((m) => m.run_id === runId && m.role === "assistant")
      if (existing) {
        if (existing.content !== content) updateMessage(existing.id, { content })
        return
      }
      addMessage({
        id: crypto.randomUUID(), role: "assistant", content,
        timestamp: new Date().toISOString(), session_id: sessionId, run_id: runId,
        ...(streamId ? { type: `stream:${streamId}` } : {}),
      } as Message)
    }

    const parse = (event: MessageEvent) => {
      try {
        const envelope = JSON.parse(event.data) as ApplicationEventEnvelope
        const type = envelope.event_type || event.type
        const data: Record<string, unknown> = envelope.payload ?? (envelope as Record<string, unknown>)
        const errorDetail = (data.error_detail || envelope.error_detail) as ExecutionError | undefined
        const sequence = envelope.session_seq
        if (typeof sequence === "number") {
          if (sequence <= lastSeqRef.current) return
          lastSeqRef.current = sequence
        }
        const runId = envelope.run_id || String(data.run_id || "")
        if (runId && typeof sequence === "number") {
          appendRunTimelineEvent({
            session_seq: sequence,
            event_type: type,
            occurred_at: envelope.occurred_at || new Date().toISOString(),
            run_id: runId,
            payload: data as RunTimelineEvent["payload"],
          })
        }
        const streamId = envelope.stream_id || String(data.stream_id || (runId ? `answer-${runId}` : ""))
        const now = new Date().toISOString()
        const timelineEntry = projectTimelineEvent({
          type,
          data,
          envelope,
          runId,
          timestamp: envelope.occurred_at || now,
        })
        if (timelineEntry) upsertExecutionTimelineEntry(timelineEntry)
        if (type === "message.persisted" && Array.isArray(data.attachment_ids) && data.attachment_ids.length > 0) {
          void apiClient.listAttachments(sessionId).then((items) => {
            if (useChatStore.getState().activeSessionId === sessionId) setAttachments(items)
          }).catch(() => {})
        }

        if (type === "stream.snapshot") {
          const version = Number(data.version || 0)
          if (data.stream_type === "thinking") {
            // Thinking is a durable event stream, never a recoverable snapshot.
            return
          }
          const stream = streamsRef.current.get(streamId) || {
            nextChunkId: 1, finalChunkId: null, content: "", status: "created",
            pending: new Map<number, string>(), snapshotVersion: 0,
          }
          if (version >= stream.snapshotVersion) {
            stream.content = String(data.content || "")
            stream.snapshotVersion = version
            stream.status = data.status === "completed" ? "completed" : "streaming"
            stream.pending.clear()
            stream.nextChunkId = Number(data.last_chunk_id || 0) + 1
            streamsRef.current.set(streamId, stream)
            upsertAssistant(runId, stream.content, streamId)
          }
          return
        }

        if (type === "message.started") {
          const stream = streamsRef.current.get(streamId) || {
            nextChunkId: 1, finalChunkId: null, content: "", status: "created",
            pending: new Map<number, string>(), snapshotVersion: 0,
          }
          streamsRef.current.set(streamId, stream)
          upsertAssistant(runId, stream.content, streamId)
        } else if (type === "message.delta") {
          const stream = streamsRef.current.get(streamId) || {
            nextChunkId: 1, finalChunkId: null, content: "", status: "created",
            pending: new Map<number, string>(), snapshotVersion: 0,
          }
          const delta = String(data.delta || data.token || "")
          const chunkId = envelope.chunk_id ?? Number(data.chunk_id || 0)
          if (!delta) return
          if (chunkId > 0) {
            if (chunkId < stream.nextChunkId) return
            stream.pending.set(chunkId, delta)
            applyPendingChunks(stream)
          } else {
            stream.content += delta
          }
          stream.status = "streaming"
          if (envelope.is_complete) {
            stream.finalChunkId = chunkId || stream.nextChunkId - 1
            stream.status = "completed"
          }
          streamsRef.current.set(streamId, stream)
          upsertAssistant(runId, stream.content, streamId)
        } else if (type === "message.completed") {
          const stream = streamsRef.current.get(streamId)
          const legacyContent = String(data.content || "")
          if (stream) {
            stream.status = "completed"
            // v2 completion is a lifecycle marker; content comes from chunks/snapshot.
            if (!stream.content && legacyContent) stream.content = legacyContent
            upsertAssistant(runId, stream.content, streamId)
          } else if (legacyContent) {
            upsertAssistant(runId, legacyContent, streamId)
          }
        } else if (type === "thinking.started" || type === "thinking.summary") {
          setThinking({ active: true, content: String(data.content || ""), messageId: streamId || null })
          setAgentStatus("thinking")
        } else if (type === "thinking.completed") {
          clearThinking()
        }

        if (type === "approval.required") {
          addApproval({ approval_id: String(data.approval_id || ""), tool_name: String(data.tool_name || "tool"), arguments: (data.arguments as Record<string, unknown>) || {}, risk_level: (String(data.risk_level || "low") as "low" | "medium" | "high"), approval_batch_id: data.approval_batch_id ? String(data.approval_batch_id) : null, session_id: sessionId, plan_id: text(data.plan_id) || undefined, task_id: text(data.task_id) || undefined, task_title: text(data.task_title) || undefined, worker_run_id: text(data.worker_run_id) || undefined })
          setAgentStatus("waiting_approval")
        } else if (type === "approval.resolved") {
          const decisions = (data.decisions as Record<string, string> | undefined) || {}
          const approvalId = String(data.approval_id || "")
          if (approvalId) decisions[approvalId] = String(data.decision || "denied")
          Object.entries(decisions).forEach(([id, decision]) => resolveApproval(id, decision))
          if (useChatStore.getState().pendingApprovals.length <= Object.keys(decisions).length) setAgentStatus("running")
        }
        if (type.startsWith("plan.")) {
          const plan: OrchestrationPlan = {
            plan_id: text(data.plan_id) || `plan:${runId}`,
            run_id: runId || undefined,
            goal: text(data.goal) || text(data.user_goal) || undefined,
            status: type === "plan.created" ? "running" : type === "plan.completed" ? "completed" : type === "plan.failed" ? "failed" : "cancelled",
            max_parallelism: number(data.max_parallelism),
            task_ids: stringArray(data.task_ids),
            session_seq: sequence,
            transition_id: envelope.transition_id,
            metadata: { ...data },
          }
          upsertOrchestrationPlan(plan)
        }
        if (type.startsWith("synthesis.")) {
          const planId = text(data.plan_id) || findPlanId(runId)
          if (planId) upsertOrchestrationPlan({ plan_id: planId, run_id: runId || undefined, status: type === "synthesis.started" ? "running" : "completed", synthesis_status: type === "synthesis.started" ? "running" : "completed", session_seq: sequence, transition_id: envelope.transition_id })
        }
        if (
          type === "task.queued" ||
          type === "task.started" ||
          type === "task.retrying" ||
          type === "task.waiting_approval" ||
          type === "task.completed" ||
          type === "task.failed" ||
          type === "task.cancelled"
        ) {
          const status: OrchestrationTask["status"] =
            type === "task.queued" ? "queued"
            : type === "task.started" ? "running"
            : type === "task.retrying" ? "retry_wait"
            : type === "task.waiting_approval" ? "waiting_approval"
            : type === "task.completed" ? "completed"
            : type === "task.cancelled" ? "cancelled"
            : "failed"
          const taskId = text(data.task_id) || crypto.randomUUID()
          const planId = text(data.plan_id) || findPlanId(runId) || `plan:${runId}`
          upsertOrchestrationTask({
            plan_id: planId,
            task_id: taskId,
            run_id: runId || undefined,
            title: text(data.title) || text(data.objective) || "",
            status,
            objective: text(data.objective) || undefined,
            worker_type: text(data.worker_type) || undefined,
            parent_run_id: envelope.parent_run_id || text(data.parent_run_id) || null,
            execution_generation: number(data.execution_generation),
            attempt: number(data.attempt),
            worker_run_id: text(data.worker_run_id) || undefined,
            started_at: text(data.started_at) || (type === "task.started" ? now : undefined),
            completed_at: text(data.completed_at) || (["completed", "failed", "cancelled"].includes(status) ? now : undefined),
            duration_ms: number(data.duration_ms),
            result_summary: summarizeResult(data.result ?? data.output),
            dependencies: stringArray(data.dependencies),
            blocked_by: stringArray(data.blocked_by),
            approval_batch_id: text(data.approval_batch_id) || null,
            error: text(data.error) || errorDetail?.message || undefined,
            error_detail: errorDetail || null,
            session_seq: sequence,
            transition_id: envelope.transition_id,
            metadata: { ...data },
          })
        }
        if (type === "llm.started") {
          const stepId = String(data.call_id || crypto.randomUUID())
          addStep({ id: stepId, session_id: sessionId, run_id: runId, parent_run_id: envelope.parent_run_id || text(data.parent_run_id) || null, step_number: useChatStore.getState().steps.length + 1, step_type: "llm_call", status: "running", started_at: now, duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 })
          setAgentStatus("thinking")
        } else if (type === "llm.completed") {
          const stepId = String(data.call_id || "")
          if (stepId) updateStep(stepId, { status: data.status === "failed" ? "failed" : "completed", completed_at: now, duration_ms: Number(data.duration_ms || 0), error_message: data.error ? String(data.error) : null, error_detail: errorDetail || null })
        } else if (type === "tool.started") {
          const toolId = String(data.tool_call_id || crypto.randomUUID())
          const stepId = String(data.call_id || crypto.randomUUID())
          addStep({ id: stepId, session_id: sessionId, run_id: runId, parent_run_id: envelope.parent_run_id || text(data.parent_run_id) || null, step_number: useChatStore.getState().steps.length + 1, step_type: "tool_execution", status: "running", started_at: now, duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 })
          addToolCall({ id: toolId, tool_name: String(data.tool_name || "tool"), arguments: (data.arguments as Record<string, unknown>) || {}, status: "running", started_at: now, risk_level: (String(data.risk_level || "low") as ToolCall["risk_level"]), step_id: stepId, run_id: runId })
        } else if (type === "tool.completed") {
          const toolId = String(data.tool_call_id || "")
          const stepId = String(data.call_id || "")
          const status = data.status === "success" ? "success" : "failed"
          if (toolId) updateToolCall(toolId, { status, completed_at: now, duration_ms: Number(data.duration_ms || 0), output: data.output ? String(data.output) : undefined, error: data.error ? String(data.error) : undefined, error_detail: errorDetail || null })
          if (stepId) updateStep(stepId, { status: status === "success" ? "completed" : "failed", completed_at: now, duration_ms: Number(data.duration_ms || 0), error_message: data.error ? String(data.error) : null, error_detail: errorDetail || null })
        }
        if (type === "run.started" || type === "run.resumed") {
          setAgentStatus("running")
          updateSession(sessionId, { status: "running" })
        } else if (type === "run.paused") {
          // A run-level terminal/control event must also close any child
          // activity that did not get its own completion event. Otherwise the
          // progress panel keeps spinning after pause.
          const terminalStatus = "paused"
          const completedAt = now
          for (const step of useChatStore.getState().steps) {
            if (step.run_id === runId && step.status === "running") {
              updateStep(step.id, { status: terminalStatus, completed_at: completedAt })
            }
          }
          for (const toolCall of useChatStore.getState().toolCalls) {
            if (toolCall.run_id === runId && toolCall.status === "running") {
              updateToolCall(toolCall.id, { status: terminalStatus, completed_at: completedAt })
            }
          }
          setAgentStatus("paused")
          updateSession(sessionId, { status: "interrupted" })
          clearThinking()
        } else if (type === "run.cancelled") {
          const completedAt = now
          for (const step of useChatStore.getState().steps) {
            if (step.run_id === runId && step.status === "running") {
              updateStep(step.id, { status: "failed", completed_at: completedAt, error_message: "Stopped by user" })
            }
          }
          for (const toolCall of useChatStore.getState().toolCalls) {
            if (toolCall.run_id === runId && toolCall.status === "running") {
              updateToolCall(toolCall.id, { status: "failed", completed_at: completedAt, error: "Stopped by user" })
            }
          }
          setAgentStatus("idle")
          updateSession(sessionId, { status: "interrupted" })
          clearApprovals()
          clearThinking()
        }
        else if (type === "run.root_suspended") {
          // The root run can wait while workers continue. Clear transient
          // thinking state without presenting the whole plan as failed.
          clearThinking()
          setAgentStatus("running")
          updateSession(sessionId, { status: "running" })
        }
        else if (type === "run.failed" || type === "run.budget_exceeded") {
          setAgentStatus("error")
          setErrorDetail(errorDetail || null)
          updateSession(sessionId, { status: "interrupted" })
          clearThinking()
          const errorMessage = text(errorDetail?.message)
            || text((data.error as Record<string, unknown> | undefined)?.message)
            || text(data.message)
            || (type === "run.budget_exceeded" ? "运行已达到限制" : "任务未能完成")
          setError(errorMessage)
        }
        else if (type === "run.completed") {
          setAgentStatus("idle")
          updateSession(sessionId, { status: "completed" })
          clearThinking()
        }
      } catch { /* 单条事件格式错误不应中断 EventSource。 */ }
    }

    ;[
      "run.started", "run.resumed", "run.paused", "run.failed", "run.cancelled", "run.completed", "run.budget_exceeded", "run.root_suspended",
      "node.started", "node.completed", "node.failed",
      "llm.started", "llm.completed", "tool.started", "tool.completed",
      "approval.required", "approval.resolved",
      "message.started", "message.persisted", "message.completed", "message.delta", "stream.snapshot",
      "thinking.started", "thinking.summary", "thinking.completed",
      "plan.created", "plan.completed", "plan.failed", "plan.cancelled",
      "task.queued", "task.started", "task.retrying", "task.waiting_approval", "task.completed", "task.failed", "task.cancelled",
      "synthesis.started", "synthesis.completed",
      "agent.waiting_for_files", "attachment_updated",
      "file_processing_started", "file_processing_completed", "file_processing_failed",
    ].forEach((name) => source.addEventListener(name, parse))
    return () => { source.close(); if (sourceRef.current === source) sourceRef.current = null }
  }, [sessionId, apiBase, setConnectionStatus, setAgentStatus, addMessage, updateMessage, addStep, updateStep, addToolCall, updateToolCall, addApproval, mergeApprovals, resolveApproval, updateSession, clearSteps, clearToolCalls, clearApprovals, clearThinking, setThinking, setError, setErrorDetail, clearErrorDetail, setAttachments, upsertOrchestrationTask, clearOrchestrationTasks, upsertOrchestrationPlan, clearOrchestrationPlans, upsertExecutionTimelineEntry, clearExecutionTimeline, appendRunTimelineEvent, clearRunTimelineEvents])

  return sourceRef
}

interface TimelineEventInput {
  type: string
  data: Record<string, unknown>
  envelope: ApplicationEventEnvelope
  runId: string
  timestamp: string
}

function projectTimelineEvent({ type, data, envelope, runId, timestamp }: TimelineEventInput): ExecutionTimelineEntry | null {
  const id = (suffix: string) => envelope.transition_id || `${suffix}:${envelope.session_seq ?? crypto.randomUUID()}`
  const error = text(data.error) || text((data.error_detail as Record<string, unknown> | undefined)?.message)
  const detail = error || undefined
  const base = (entry: Omit<ExecutionTimelineEntry, "id" | "timestamp" | "run_id" | "event_type" | "session_seq"> & { id?: string }): ExecutionTimelineEntry => ({
    id: entry.id || id(type),
    session_seq: envelope.session_seq,
    run_id: runId || undefined,
    event_type: type,
    timestamp,
    ...entry,
  })

  // Internal model/node lifecycle events have no user-actionable detail.
  // Showing each one as a timeline row produces repetitive, misleading steps.
  if (type.startsWith("thinking.") || type.startsWith("node.")) return null

  if (type === "run.started" || type === "run.resumed") return null
  if (type === "run.completed") return null
  if (type === "run.paused") return base({ id: `run:${runId}`, label: "任务已暂停", status: "paused" })
  if (type === "run.cancelled") return base({ id: `run:${runId}`, label: "任务已取消", status: "info" })
  if (type === "run.root_suspended") return base({ id: `run:${runId}`, label: "主任务等待子任务继续", detail: "并行任务仍在处理，完成后将继续汇总结果。", status: "waiting" })
  if (type === "run.failed" || type === "run.budget_exceeded") return base({ id: `run:${runId}`, label: type === "run.budget_exceeded" ? "任务已达到运行限制" : "任务未能完成", detail, status: "failed" })

  if (type.startsWith("llm.")) return null

  if (type === "tool.started") {
    const toolName = text(data.tool_name)
    return base({
      id: `tool:${text(data.tool_call_id)}`,
      label: toolActionLabel(toolName, "started"),
      status: "running",
      metadata: { ...(toolName ? { tool_name: toolName } : {}), ...(data.arguments ? { arguments: data.arguments } : {}) },
    })
  }
  if (type === "tool.completed") {
    const toolName = text(data.tool_name)
    const failed = Boolean(error) || data.status === "failed"
    return base({
      id: `tool:${text(data.tool_call_id)}`,
      label: toolActionLabel(toolName, failed ? "failed" : "completed"),
      detail: error || undefined,
      status: failed ? "failed" : "completed",
      metadata: { ...(toolName ? { tool_name: toolName } : {}), ...(data.output != null ? { output: data.output } : {}) },
    })
  }

  if (type === "approval.required") {
    const toolName = text(data.tool_name)
    const args = (data.arguments as Record<string, unknown> | undefined) || {}
    return base({ id: `approval:${text(data.approval_id)}`, label: "需要你确认这项操作", detail: approvalActionDescription(toolName, args), status: "waiting", metadata: { ...(toolName ? { tool_name: toolName } : {}), ...(data.arguments ? { arguments: data.arguments } : {}) } })
  }
  if (type === "approval.resolved") {
    const decision = text(data.decision)
    const detail = decision === "approved" ? "你已允许此操作继续。" : decision === "cancelled" ? "此操作已取消，未继续执行。" : "你已拒绝此操作，未继续执行。"
    return base({ id: `approval:${text(data.approval_id)}`, label: "确认已提交", detail, status: "completed" })
  }

  if (type.startsWith("plan.")) {
    const planId = text(data.plan_id)
    const labels: Record<string, string> = { "plan.created": "已制定执行计划", "plan.completed": "执行计划已完成", "plan.failed": "执行计划未能完成", "plan.cancelled": "执行计划已取消" }
    return base({ id: `plan:${planId}`, label: labels[type] || "执行计划已更新", detail: error || undefined, status: type === "plan.failed" ? "failed" : type === "plan.created" ? "running" : "completed" })
  }
  if (type.startsWith("task.")) return null
  if (type.startsWith("synthesis.")) return base({ id: `synthesis:${text(data.plan_id)}`, label: type === "synthesis.started" ? "正在汇总处理结果" : "处理结果已汇总", status: type === "synthesis.started" ? "running" : "completed" })

  if (type === "agent.waiting_for_files") return base({ id: `files:${runId}`, label: "等待文件处理完成后继续", status: "waiting" })
  if (type.startsWith("file_processing")) {
    const attachmentId = text(data.attachment_id)
    const status = type.endsWith("failed") ? "failed" : type.endsWith("completed") ? "completed" : "running"
    const label = type.endsWith("failed") ? "文件处理失败" : type.endsWith("completed") ? "文件已处理完成" : "正在处理文件"
    return base({ id: `file:${attachmentId}`, label, detail, status })
  }
  if (type === "attachment_updated") return base({ id: `attachment:${text(data.attachment_id)}`, label: "附件状态已更新", status: "info" })

  return null
}

function text(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function number(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value)
  return undefined
}

function stringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined
  const items = value.map((item) => typeof item === "string" ? item : "").filter(Boolean)
  return items.length > 0 ? items : undefined
}

function findPlanId(runId: string): string | undefined {
  if (!runId) return undefined
  return useChatStore.getState().orchestrationPlans.find((plan) => plan.run_id === runId)?.plan_id
}
