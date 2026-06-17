import { api } from '../client'
import { createSSEStream, type SSECallbacks } from '../sse'

export interface MessageRequest {
  content: string
  session_id?: string | null
  attachments?: Record<string, unknown>[]
}

export interface ConfirmRequest {
  task_id: string
  step: number
  approved: boolean
}

export function sendMessage(body: MessageRequest, callbacks: SSECallbacks): AbortController {
  return createSSEStream('/api/v1/im/web/message', body, callbacks)
}

export async function sendConfirm(body: ConfirmRequest): Promise<{
  code: number
  message: string
  data: { task_id: string; step: number; approved: boolean }
}> {
  return api.post('/im/web/message/confirm', body)
}
