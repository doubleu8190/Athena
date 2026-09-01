import { useEffect, useRef } from "react"
import { useChatStore } from "../store/chatStore"
import type { ApplicationEventEnvelope } from "../types/events"
import type { Message, ToolCall } from "../types"

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
    addStep, updateStep, addToolCall, updateToolCall, addApproval, resolveApproval,
    clearSteps, clearToolCalls, clearApprovals, clearThinking, setThinking, setError,
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
    if (!sessionId) {
      setConnectionStatus("disconnected")
      return
    }

    const url = `${apiBase.replace(/\/$/, "")}/api/sessions/${encodeURIComponent(sessionId)}/events`
    const source = new EventSource(url, { withCredentials: true })
    sourceRef.current = source
    setConnectionStatus("connecting")
    source.onopen = () => setConnectionStatus("connected")
    source.onerror = () => setConnectionStatus("error")

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
        const sequence = envelope.session_seq
        if (sequence !== undefined) {
          if (sequence <= lastSeqRef.current) return
          lastSeqRef.current = sequence
        }
        const runId = envelope.run_id || String(data.run_id || "")
        const streamId = envelope.stream_id || String(data.stream_id || (runId ? `answer-${runId}` : ""))
        const now = new Date().toISOString()

        if (type === "stream.snapshot") {
          const version = Number(data.version || 0)
          if (data.stream_type === "thinking") {
            setThinking({
              active: data.status !== "completed",
              content: String(data.content || ""),
              messageId: streamId || null,
            })
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
          addApproval({ approval_id: String(data.approval_id || ""), tool_name: String(data.tool_name || "tool"), arguments: (data.arguments as Record<string, unknown>) || {}, risk_level: (String(data.risk_level || "low") as "low" | "medium" | "high"), timeout: Number(data.timeout || 120), session_id: sessionId })
          setAgentStatus("waiting_approval")
        } else if (type === "approval.resolved" || type === "approval.expired") {
          const approvalId = String(data.approval_id || "")
          if (approvalId) resolveApproval(approvalId, String(data.decision || "denied"))
        }
        if (type === "llm.started") {
          const stepId = String(data.call_id || crypto.randomUUID())
          addStep({ id: stepId, session_id: sessionId, run_id: runId, step_number: useChatStore.getState().steps.length + 1, step_type: "llm_call", status: "running", started_at: now, duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 })
          setAgentStatus("thinking")
        } else if (type === "llm.completed") {
          const stepId = String(data.call_id || "")
          if (stepId) updateStep(stepId, { status: data.status === "failed" ? "failed" : "completed", completed_at: now, duration_ms: Number(data.duration_ms || 0), error_message: data.error ? String(data.error) : null })
        } else if (type === "tool.started") {
          const toolId = String(data.tool_call_id || crypto.randomUUID())
          const stepId = String(data.call_id || crypto.randomUUID())
          addStep({ id: stepId, session_id: sessionId, run_id: runId, step_number: useChatStore.getState().steps.length + 1, step_type: "tool_execution", status: "running", started_at: now, duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 })
          addToolCall({ id: toolId, tool_name: String(data.tool_name || "tool"), arguments: (data.arguments as Record<string, unknown>) || {}, status: "running", started_at: now, risk_level: (String(data.risk_level || "low") as ToolCall["risk_level"]), step_id: stepId, run_id: runId })
        } else if (type === "tool.completed") {
          const toolId = String(data.tool_call_id || "")
          const stepId = String(data.call_id || "")
          const status = data.status === "success" ? "success" : "failed"
          if (toolId) updateToolCall(toolId, { status, completed_at: now, duration_ms: Number(data.duration_ms || 0), output: data.output ? String(data.output) : undefined, error: data.error ? String(data.error) : undefined })
          if (stepId) updateStep(stepId, { status: status === "success" ? "completed" : "failed", completed_at: now, duration_ms: Number(data.duration_ms || 0), error_message: data.error ? String(data.error) : null })
        }
        if (type === "run.started" || type === "run.resumed") setAgentStatus("running")
        else if (type === "run.paused" || type === "run.cancelled") { setAgentStatus("idle"); clearThinking() }
        else if (type === "run.failed") { setAgentStatus("error"); setError(String(data.error || "Run failed")) }
        else if (type === "run.completed") { setAgentStatus("idle"); clearThinking() }
      } catch { /* 单条事件格式错误不应中断 EventSource。 */ }
    }

    ;["run.started", "run.resumed", "run.paused", "run.failed", "run.cancelled", "run.completed", "llm.started", "llm.completed", "tool.started", "tool.completed", "approval.required", "approval.resolved", "approval.expired", "message.started", "message.completed", "message.delta", "stream.snapshot", "thinking.started", "thinking.summary", "thinking.completed"].forEach((name) => source.addEventListener(name, parse))
    return () => { source.close(); if (sourceRef.current === source) sourceRef.current = null }
  }, [sessionId, apiBase, setConnectionStatus, setAgentStatus, addMessage, updateMessage, addStep, updateStep, addToolCall, updateToolCall, addApproval, resolveApproval, clearSteps, clearToolCalls, clearApprovals, clearThinking, setThinking, setError])

  return sourceRef
}
