import { useCallback } from 'react'
import { sendMessage, type MessageRequest } from '../api/endpoints/chat'
import type { SSECallbacks } from '../api/sse'

export function useSSE() {
  const streamMessage = useCallback(
    (body: MessageRequest, callbacks: SSECallbacks): AbortController => {
      return sendMessage(body, callbacks)
    },
    []
  )

  return { streamMessage }
}
