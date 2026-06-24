import { api } from '../client'
import { createSSEClient, type SSEEventCallback } from '../sse'

// ── Types ─────────────────────────────────────────────────────────────

export interface MessageRequest {
  content: string
  session_id: string
  attachments?: Record<string, unknown>[]
}

export interface ConfirmRequest {
  task_id: string
  step: number
  approved: boolean
}

// ── Send message via SSE ───────────────────────────────────────────────

export function sendMessage(
  body: MessageRequest,
  onEvent: SSEEventCallback,
  onDone?: () => void,
  onError?: (err: Error) => void
): { abort: () => void } {
  return createSSEClient('/api/v1/im/web/message', body, {
    onEvent,
    onDone,
    onError,
  })
}

// ── Confirm subtask ────────────────────────────────────────────────────

export function confirmSubtask(body: ConfirmRequest): Promise<{ code: number; message: string }> {
  return api.post('/im/web/message/confirm', body)
}
