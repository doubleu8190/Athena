/**
 * POST-SSE client — streams text/event-stream from a fetch ReadableStream.
 *
 * Standard EventSource only supports GET, but Athena's message endpoint
 * uses POST. This client wraps fetch() + ReadableStream to parse SSE events.
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL || '/api/v1'

export type SSEEventCallback = (eventType: string, data: Record<string, unknown>) => void
export type SSEDoneCallback = () => void
export type SSEErrorCallback = (error: Error) => void

export interface SSEClientOptions {
  onEvent: SSEEventCallback
  onDone?: SSEDoneCallback
  onError?: SSEErrorCallback
}

export function createSSEClient(
  path: string,
  body: unknown,
  options: SSEClientOptions
): { abort: () => void } {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  const controller = new AbortController()
  const { onEvent, onDone, onError } = options

  let aborted = false

  const run = async () => {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      })

      if (!response.ok) {
        const errBody = await response.json().catch(() => ({}))
        throw new Error(
          (errBody as { message?: string }).message ||
            `HTTP ${response.status} ${response.statusText}`
        )
      }

      if (!response.body) {
        throw new Error('No response body — SSE requires a streaming response')
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // SSE events are separated by double newlines
        const parts = buffer.split('\n\n')
        // The last part may be incomplete
        buffer = parts.pop() || ''

        for (const part of parts) {
          if (!part.trim()) continue
          const eventType = extractLine(part, 'event') || 'message'
          const dataStr = extractLine(part, 'data')
          if (dataStr) {
            try {
              const data = JSON.parse(dataStr)
              onEvent(eventType, data)
            } catch {
              onEvent(eventType, { raw: dataStr })
            }
          }
        }
      }

      if (!aborted) {
        onDone?.()
      }
    } catch (err) {
      if (aborted) return
      if (err instanceof DOMException && err.name === 'AbortError') return
      onError?.(err instanceof Error ? err : new Error(String(err)))
    }
  }

  run()

  return {
    abort: () => {
      aborted = true
      controller.abort()
    },
  }
}

function extractLine(chunk: string, prefix: string): string | null {
  const line = chunk
    .split('\n')
    .find((l) => l.startsWith(`${prefix}:`))
  if (!line) return null
  return line.slice(prefix.length + 1).trim()
}
