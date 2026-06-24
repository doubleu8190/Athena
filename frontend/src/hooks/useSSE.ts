import { useState, useRef, useCallback } from 'react'
import { sendMessage, confirmSubtask } from '../api/endpoints/chat'
import type { ConfirmRequest } from '../api/endpoints/chat'
import { useChatStore, type SubtaskEvent } from '../stores/chatStore'

/**
 * Hook for sending a message and managing the SSE stream lifecycle.
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

      const { abort } = sendMessage(
        { content, session_id: sessionId },
        (eventType, data) => {
          const store = useChatStore.getState()

          switch (eventType) {
            case 'plan_generating':
              store.updateMessage(sessionId, assistantMsg.id, {
                taskStatus: 'generating_plan',
              })
              break

            case 'plan_generated': {
              store.updateMessage(sessionId, assistantMsg.id, {
                taskStatus: 'executing',
              })
              break
            }

            case 'subtask_started': {
              const step = data.step as number
              const subtask: SubtaskEvent = {
                step,
                tool_name: data.tool_name as string,
                intent: data.intent as string,
                status: 'running',
              }
              const msg = store.messages[sessionId]?.find((m) => m.id === assistantMsg.id)
              const subtasks = [...(msg?.subtasks || []), subtask]
              store.updateMessage(sessionId, assistantMsg.id, { subtasks })
              break
            }

            case 'subtask_completed':
            case 'subtask_failed':
            case 'subtask_skipped':
            case 'subtask_fallback': {
              const step = data.step as number
              const msg = store.messages[sessionId]?.find((m) => m.id === assistantMsg.id)
              const subtasks = (msg?.subtasks || []).map((st) =>
                st.step === step ? { ...st, ...data, status: eventType.replace('subtask_', '') } : st
              )
              store.updateMessage(sessionId, assistantMsg.id, { subtasks })
              break
            }

            case 'confirm_required': {
              store.updateMessage(sessionId, assistantMsg.id, {
                confirmRequired: {
                  task_id: data.task_id as string,
                  step: data.step as number,
                  risk_level: (data.risk_level as string) || 'medium',
                  preview_text: (data.preview_text as string) || '',
                  cooling_off_seconds: (data.cooling_off_seconds as number) || 0,
                  timeout_seconds: (data.timeout_seconds as number) || 60,
                },
              })
              break
            }

            case 'task_completed':
              store.updateMessage(sessionId, assistantMsg.id, {
                isStreaming: false,
                taskStatus: 'completed',
                content: (data.summary as string) || 'Task completed',
              })
              break

            case 'task_failed':
              store.updateMessage(sessionId, assistantMsg.id, {
                isStreaming: false,
                taskStatus: 'failed',
                content: `Error: ${data.error || 'Task failed'}`,
              })
              break

            case 'error':
              store.updateMessage(sessionId, assistantMsg.id, {
                isStreaming: false,
                taskStatus: 'failed',
                content: `Error: ${(data.message as string) || 'Unknown error'}`,
              })
              break
          }
        },
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

  const confirm = useCallback(async (req: ConfirmRequest) => {
    await confirmSubtask(req)
  }, [])

  return { isStreaming, startStream, stopStream, confirm }
}
