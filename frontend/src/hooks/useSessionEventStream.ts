import { useEffect, useRef } from "react"
import { useChatStore } from "../store/chatStore"
import { apiClient } from "../api/client"
import type { ApplicationEventEnvelope, ExecutionError } from "../types/events"
import type { ExecutionTimelineEntry, Message, ToolCall } from "../types"

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
    setConnectionStatus, setAgentStatus, addMessage, updateMessage,
    addStep, updateStep, addToolCall, updateToolCall, addApproval, mergeApprovals, resolveApproval,
    updateSession,
    clearSteps, clearToolCalls, clearApprovals, clearThinking, setThinking, setError, setErrorDetail, clearErrorDetail,
    upsertOrchestrationTask, clearOrchestrationTasks,
    upsertExecutionTimelineEntry, clearExecutionTimeline,
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
    clearExecutionTimeline()
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
            timeout: approval.timeout || 120,
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
          addApproval({ approval_id: String(data.approval_id || ""), tool_name: String(data.tool_name || "tool"), arguments: (data.arguments as Record<string, unknown>) || {}, risk_level: (String(data.risk_level || "low") as "low" | "medium" | "high"), timeout: Number(data.timeout || 120), expires_at: data.expires_at ? String(data.expires_at) : null, session_id: sessionId })
          setAgentStatus("waiting_approval")
        } else if (type === "approval.resolved" || type === "approval.expired") {
          const approvalId = String(data.approval_id || "")
          if (approvalId) resolveApproval(approvalId, String(data.decision || "denied"))
          if (useChatStore.getState().pendingApprovals.length <= 1) setAgentStatus("running")
        }
        if (
          type === "task.queued" ||
          type === "task.started" ||
          type === "task.retrying" ||
          type === "task.completed" ||
          type === "task.failed"
        ) {
          const status =
            type === "task.queued" ? "queued"
            : type === "task.started" ? "running"
            : type === "task.retrying" ? "retry_wait"
            : type === "task.completed" ? "completed"
            : "failed"
          upsertOrchestrationTask({
            plan_id: String(data.plan_id || ""),
            task_id: String(data.task_id || crypto.randomUUID()),
            title: String(data.title || data.task_id || "Task"),
            status,
            attempt: data.attempt ? Number(data.attempt) : undefined,
            worker_run_id: data.worker_run_id ? String(data.worker_run_id) : undefined,
            error: data.error ? String(data.error) : undefined,
          })
        }
        if (type === "llm.started") {
          const stepId = String(data.call_id || crypto.randomUUID())
          addStep({ id: stepId, session_id: sessionId, run_id: runId, step_number: useChatStore.getState().steps.length + 1, step_type: "llm_call", status: "running", started_at: now, duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 })
          setAgentStatus("thinking")
        } else if (type === "llm.completed") {
          const stepId = String(data.call_id || "")
          if (stepId) updateStep(stepId, { status: data.status === "failed" ? "failed" : "completed", completed_at: now, duration_ms: Number(data.duration_ms || 0), error_message: data.error ? String(data.error) : null, error_detail: errorDetail || null })
        } else if (type === "tool.started") {
          const toolId = String(data.tool_call_id || crypto.randomUUID())
          const stepId = String(data.call_id || crypto.randomUUID())
          addStep({ id: stepId, session_id: sessionId, run_id: runId, step_number: useChatStore.getState().steps.length + 1, step_type: "tool_execution", status: "running", started_at: now, duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 })
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
        else if (type === "run.failed" || type === "run.budget_exceeded") {
          setAgentStatus("error")
          setErrorDetail(errorDetail || null)
          updateSession(sessionId, { status: "interrupted" })
          clearThinking()
          setError(String(errorDetail?.message || data.error || data.message || (type === "run.budget_exceeded" ? "Run budget exceeded" : "Run failed")))
        }
        else if (type === "run.completed") {
          setAgentStatus("idle")
          updateSession(sessionId, { status: "completed" })
          clearThinking()
        }
      } catch { /* 单条事件格式错误不应中断 EventSource。 */ }
    }

    ;[
      "run.started", "run.resumed", "run.paused", "run.failed", "run.cancelled", "run.completed", "run.budget_exceeded",
      "node.started", "node.completed", "node.failed",
      "llm.started", "llm.completed", "tool.started", "tool.completed",
      "approval.required", "approval.resolved", "approval.expired",
      "message.started", "message.completed", "message.delta", "stream.snapshot",
      "thinking.started", "thinking.summary", "thinking.completed",
      "plan.created", "plan.completed", "plan.failed", "plan.cancelled",
      "task.queued", "task.started", "task.retrying", "task.completed", "task.failed",
      "synthesis.started", "synthesis.completed",
      "subagent.started", "subagent.completed", "subagent.failed",
      "agent.waiting_for_files", "attachment_updated",
      "file_processing_started", "file_processing_completed", "file_processing_failed",
    ].forEach((name) => source.addEventListener(name, parse))
    return () => { source.close(); if (sourceRef.current === source) sourceRef.current = null }
  }, [sessionId, apiBase, setConnectionStatus, setAgentStatus, addMessage, updateMessage, addStep, updateStep, addToolCall, updateToolCall, addApproval, mergeApprovals, resolveApproval, updateSession, clearSteps, clearToolCalls, clearApprovals, clearThinking, setThinking, setError, setErrorDetail, clearErrorDetail, upsertOrchestrationTask, clearOrchestrationTasks, upsertExecutionTimelineEntry, clearExecutionTimeline])

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

  if (type === "thinking.started" || type === "thinking.summary") {
    return base({
      id: `thinking:${envelope.session_seq ?? crypto.randomUUID()}`,
      label: text(data.content) || "Preparing response",
      status: "info",
    })
  }
  if (type === "thinking.completed") return null

  if (type === "run.started" || type === "run.resumed") return base({ id: `run:${runId}`, label: "Execution started", status: "running" })
  if (type === "run.completed") return base({ id: `run:${runId}`, label: "Execution completed", status: "completed" })
  if (type === "run.paused") return base({ id: `run:${runId}`, label: "Execution paused", status: "paused" })
  if (type === "run.cancelled") return base({ id: `run:${runId}`, label: "Execution stopped", status: "info" })
  if (type === "run.failed" || type === "run.budget_exceeded") return base({ id: `run:${runId}`, label: type === "run.budget_exceeded" ? "Execution budget reached" : "Execution failed", detail, status: "failed" })

  if (type === "node.started" || type === "node.completed" || type === "node.failed") {
    const nodeName = text(data.node_name) || "graph node"
    const executionId = text(data.execution_id) || String(envelope.session_seq ?? crypto.randomUUID())
    const status = type === "node.started" ? "running" : type === "node.failed" ? "failed" : "completed"
    const label = type === "node.started" ? `Node started: ${nodeName}` : type === "node.failed" ? `Node failed: ${nodeName}` : `Node completed: ${nodeName}`
    return base({
      id: `node:${executionId}`,
      label,
      detail,
      status,
      metadata: data.duration_ms !== undefined ? { duration_ms: data.duration_ms, node_name: nodeName } : { node_name: nodeName },
    })
  }

  if (type === "llm.started") return base({ id: `llm:${text(data.call_id)}`, label: "Generating response", status: "running" })
  if (type === "llm.completed") return base({ id: `llm:${text(data.call_id)}`, label: error ? "Response generation failed" : "Response generated", detail, status: error ? "failed" : "completed" })

  if (type === "tool.started") return base({
    id: `tool:${text(data.tool_call_id)}`,
    label: `Using tool: ${text(data.tool_name) || "tool"}`,
    status: "running",
    metadata: data.arguments ? { arguments: data.arguments } : undefined,
  })
  if (type === "tool.completed") return base({
    id: `tool:${text(data.tool_call_id)}`,
    label: error ? `Tool failed: ${text(data.tool_name) || "tool"}` : `Tool completed: ${text(data.tool_name) || "tool"}`,
    detail: error || text(data.output) || undefined,
    status: error ? "failed" : "completed",
  })

  if (type === "approval.required") return base({ id: `approval:${text(data.approval_id)}`, label: `Approval required: ${text(data.tool_name) || "tool"}`, status: "waiting", metadata: data.arguments ? { arguments: data.arguments } : undefined })
  if (type === "approval.resolved" || type === "approval.expired") return base({ id: `approval:${text(data.approval_id)}`, label: type === "approval.expired" ? "Approval expired" : "Approval resolved", status: type === "approval.expired" ? "failed" : "completed" })

  if (type.startsWith("plan.")) {
    const planId = text(data.plan_id)
    const labels: Record<string, string> = { "plan.created": "Execution plan created", "plan.completed": "Execution plan completed", "plan.failed": "Execution plan failed", "plan.cancelled": "Execution plan cancelled" }
    return base({ id: `plan:${planId}`, label: labels[type] || "Execution plan updated", detail: error || text(data.goal) || undefined, status: type === "plan.failed" ? "failed" : type === "plan.created" ? "running" : "completed" })
  }
  if (type.startsWith("task.")) {
    const taskId = text(data.task_id)
    const status = type === "task.failed" ? "failed" : type === "task.completed" ? "completed" : type === "task.started" ? "running" : "waiting"
    return base({ id: `task:${taskId}`, label: `${type === "task.completed" ? "Task completed" : type === "task.failed" ? "Task failed" : type === "task.started" ? "Task started" : "Task queued"}: ${text(data.title) || taskId || "task"}`, detail, status })
  }
  if (type.startsWith("synthesis.")) return base({ id: `synthesis:${text(data.plan_id)}`, label: type === "synthesis.started" ? "Combining task results" : "Task results combined", status: type === "synthesis.started" ? "running" : "completed" })

  if (type.startsWith("subagent.")) {
    const subRunId = text(data.sub_run_id) || runId
    const status = type === "subagent.failed" ? "failed" : type === "subagent.completed" ? "completed" : "running"
    const label = type === "subagent.failed" ? "Sub-agent failed" : type === "subagent.completed" ? "Sub-agent completed" : "Sub-agent started"
    return base({ id: `subagent:${subRunId}`, label, detail: error || text(data.task) || undefined, status })
  }

  if (type === "agent.waiting_for_files") return base({ id: `files:${runId}`, label: "Waiting for files to finish processing", status: "waiting" })
  if (type.startsWith("file_processing")) {
    const attachmentId = text(data.attachment_id)
    const status = type.endsWith("failed") ? "failed" : type.endsWith("completed") ? "completed" : "running"
    const label = type.endsWith("failed") ? "File processing failed" : type.endsWith("completed") ? "File processing completed" : "Processing file"
    return base({ id: `file:${attachmentId}`, label, detail, status })
  }
  if (type === "attachment_updated") return base({ id: `attachment:${text(data.attachment_id)}`, label: "File status updated", status: "info" })

  return null
}

function text(value: unknown): string {
  return typeof value === "string" ? value : ""
}
