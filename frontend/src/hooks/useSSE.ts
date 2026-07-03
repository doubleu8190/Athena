import { useState, useRef, useCallback } from 'react'
import { sendMessage, confirmAndStream } from '../api/endpoints/chat'
import type { ConfirmRequest } from '../api/endpoints/chat'
import { useChatStore, type ToolCallEvent } from '../stores/chatStore'

/**
 * Shared SSE event handler — updates the specified assistant message in the
 * store for every known event type.  Used by both ``startStream`` (new
 * message) and ``resumeStream`` (confirm + resume).
 */
function createEventHandler(sessionId: string, msgId: string) {
  return (eventType: string, data: Record<string, unknown>) => {
    const store = useChatStore.getState()

    switch (eventType) {
      // ── New: Tool-calling agent events ──────────────────────────

      case 'agent_thinking':
        store.updateMessage(sessionId, msgId, {
          taskStatus: 'thinking',
        })
        break

      case 'text_delta': {
        const msg = store.messages[sessionId]?.find((m) => m.id === msgId)
        const currentContent = msg?.content || ''
        const delta = (data.content as string) || ''
        store.updateMessage(sessionId, msgId, {
          content: currentContent + delta,
        })
        break
      }

      case 'tool_call_start': {
        const tc: ToolCallEvent = {
          tool_name: (data.tool_name as string) || 'unknown',
          args_preview: (data.args_preview as string) || '',
          status: 'running',
        }
        const msg = store.messages[sessionId]?.find((m) => m.id === msgId)
        const toolCalls = [...(msg?.toolCalls || []), tc]
        store.updateMessage(sessionId, msgId, {
          toolCalls,
          taskStatus: 'executing',
        })
        break
      }

      case 'tool_call_result': {
        const toolName = (data.tool_name as string) || 'unknown'
        const msg = store.messages[sessionId]?.find((m) => m.id === msgId)
        const toolCalls = (msg?.toolCalls || []).map((tc) =>
          tc.tool_name === toolName
            ? {
                ...tc,
                status: data.success ? 'success' : 'error',
                output_preview: (data.output_preview as string) || tc.output_preview,
              } as ToolCallEvent
            : tc
        )
        store.updateMessage(sessionId, msgId, { toolCalls })
        break
      }

      // ── Legacy: plan-execute events (backward compat) ──────────

      case 'plan_generating':
        store.updateMessage(sessionId, msgId, {
          taskStatus: 'generating_plan',
        })
        break

      case 'plan_generated': {
        store.updateMessage(sessionId, msgId, {
          taskStatus: 'executing',
        })
        break
      }

      case 'subtask_started': {
        const step = data.step as number
        const subtask = {
          step,
          tool_name: data.tool_name as string,
          intent: data.intent as string,
          status: 'running',
        }
        const msg = store.messages[sessionId]?.find((m) => m.id === msgId)
        const subtasks = [...(msg?.subtasks || []), subtask] as any[]
        store.updateMessage(sessionId, msgId, { subtasks })
        break
      }

      case 'subtask_completed':
      case 'subtask_failed':
      case 'subtask_skipped':
      case 'subtask_fallback': {
        const step = data.step as number
        const msg = store.messages[sessionId]?.find((m) => m.id === msgId)
        const subtasks = (msg?.subtasks || []).map((st) =>
          st.step === step
            ? { ...st, ...data, status: eventType.replace('subtask_', '') }
            : st
        )
        store.updateMessage(sessionId, msgId, { subtasks })
        break
      }

      // ── Shared events ───────────────────────────────────────────

      case 'confirm_required': {
        store.updateMessage(sessionId, msgId, {
          confirmRequired: {
            task_id: (data.task_id as string) || sessionId,
            step: (data.step as number) || 0,
            tool_name: (data.tool_name as string) || '',
            risk_level: (data.risk_level as string) || 'medium',
            preview_text:
              (data.args_preview as string) ||
              (data.preview_text as string) ||
              '',
            reason: (data.reason as string) || '',
            cooling_off_seconds: (data.cooling_off_seconds as number) || 0,
            timeout_seconds: (data.timeout_seconds as number) || 60,
          },
        })
        break
      }

      case 'task_completed':
        store.updateMessage(sessionId, msgId, {
          isStreaming: false,
          taskStatus: 'completed',
          content:
            (store.messages[sessionId]?.find((m) => m.id === msgId)?.content) ||
            (data.summary as string) ||
            'Task completed',
        })
        break

      case 'task_failed':
        store.updateMessage(sessionId, msgId, {
          isStreaming: false,
          taskStatus: 'failed',
          content:
            store.messages[sessionId]?.find((m) => m.id === msgId)?.content ||
            `Error: ${data.error || 'Task failed'}`,
        })
        break

      case 'error':
        store.updateMessage(sessionId, msgId, {
          isStreaming: false,
          taskStatus: 'failed',
          content:
            store.messages[sessionId]?.find((m) => m.id === msgId)?.content ||
            `Error: ${(data.message as string) || 'Unknown error'}`,
        })
        break
    }
  }
}

/**
 * Hook for sending a message and managing the SSE stream lifecycle.
 *
 * Handles both the new tool-calling agent events (agent_thinking, text_delta,
 * tool_call_start, tool_call_result) and the legacy plan-execute events
 * (plan_generating, subtask_started, etc.) for backward compatibility.
 */
export function useSSE() {
  const [isStreaming, setIsStreaming] = useState(false)
  const abortRef = useRef<(() => void) | null>(null)

  const startStream = useCallback(
    (content: string) => {
      // Prepare messages in store
      const { assistantMsg, sessionId } =
        useChatStore.getState().sendUserMessage(content)

      setIsStreaming(true)

      const handleEvent = createEventHandler(sessionId, assistantMsg.id)

      const { abort } = sendMessage(
        { content, session_id: sessionId },
        handleEvent,
        () => {
          setIsStreaming(false)
        },
        (err) => {
          useChatStore.getState().updateMessage(sessionId, assistantMsg.id, {
            isStreaming: false,
            taskStatus: 'failed',
            content: `Error: ${err.message}`,
          })
          setIsStreaming(false)
        }
      )

      abortRef.current = abort
    },
    []
  )

  const stopStream = useCallback(() => {
    abortRef.current?.()
    abortRef.current = null
    setIsStreaming(false)
  }, [])

  /**
   * Resume a paused graph execution via SSE.
   *
   * Called after the user approves/denies a ``confirm_required`` event.
   * Opens a new SSE stream to the confirm endpoint and processes the
   * resumed events (tool results, agent responses, nested confirmations,
   * etc.), updating the same assistant message in the store.
   */
  const resumeStream = useCallback(
    (req: ConfirmRequest, sessionId: string, msgId: string) => {
      setIsStreaming(true)

      // Immediately clear the confirmRequired UI so the user sees progress
      useChatStore.getState().updateMessage(sessionId, msgId, {
        confirmRequired: undefined,
      })

      const handleEvent = createEventHandler(sessionId, msgId)

      const { abort } = confirmAndStream(
        req,
        handleEvent,
        () => {
          setIsStreaming(false)
        },
        (err) => {
          useChatStore.getState().updateMessage(sessionId, msgId, {
            isStreaming: false,
            taskStatus: 'failed',
            content: `Error: ${err.message}`,
          })
          setIsStreaming(false)
        }
      )

      abortRef.current = abort
    },
    []
  )

  return { isStreaming, startStream, stopStream, resumeStream }
}
