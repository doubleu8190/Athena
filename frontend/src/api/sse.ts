import { useAuthStore } from '../stores/authStore'

export interface SSECallbacks {
  onEvent: (eventType: string, data: Record<string, unknown>) => void
  onError: (error: Error) => void
  onDone: () => void
}

export function createSSEStream(
  url: string,
  body: unknown,
  callbacks: SSECallbacks
): AbortController {
  const controller = new AbortController()
  const apiKey = useAuthStore.getState().apiKey
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (apiKey) headers['X-API-Key'] = apiKey

  const { onEvent, onError, onDone } = callbacks

  fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        let errMsg = `HTTP ${response.status}`
        try {
          const errBody = await response.json()
          errMsg = errBody.message || errBody.detail || errMsg
        } catch {
          // use default
        }
        throw new Error(errMsg)
      }

      const reader = response.body?.getReader()
      if (!reader) {
        throw new Error('ReadableStream not supported')
      }

      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // Parse SSE events from buffer
        const lines = buffer.split('\n')
        buffer = lines.pop() || '' // keep incomplete line in buffer

        let currentEvent = ''
        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            const rawData = line.slice(6)
            try {
              const data = JSON.parse(rawData)
              if (currentEvent) {
                onEvent(currentEvent, data)
              }
            } catch {
              // Skip unparseable events
            }
            currentEvent = ''
          }
          // Empty lines or comment lines are ignored
        }
      }
    })
    .then(() => {
      onDone()
    })
    .catch((err) => {
      if (err.name === 'AbortError') {
        onDone()
        return
      }
      onError(err instanceof Error ? err : new Error(String(err)))
    })

  return controller
}
