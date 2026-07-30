import { api } from '../client'
import { createSSEClient, type SSEEventCallback } from '../sse'
import { AthenaWSClient, createWSClient, type WSMessage } from '../ws'
import type { Message } from '../../stores/chatStore'

// ── Types ─────────────────────────────────────────────────────────────

export interface MessageRequest {
  content: string
  chat_id: string
  attachments?: Record<string, unknown>[]
}

export interface ConfirmRequest {
  chat_id: string
  approved: boolean
}

export interface HistoryResponse {
  messages: Message[]
}

export interface SessionInfo {
  id: string
  title: string
  createdAt: number
}

export interface SessionsResponse {
  sessions: SessionInfo[]
}

// ── Send message via SSE ───────────────────────────────────────────────

export function sendMessage(
  body: MessageRequest,
  onEvent: SSEEventCallback,
  onDone?: () => void,
  onError?: (err: Error) => void
): { abort: () => void } {
  return createSSEClient('/im/web/message', body, {
    onEvent,
    onDone,
    onError,
  })
}

// ── Confirm subtask (JSON, legacy) ──────────────────────────────────────

export function confirmSubtask(body: ConfirmRequest): Promise<{ code: number; message: string }> {
  return api.post('/im/web/message/confirm', body)
}

// ── Confirm + resume SSE stream ──────────────────────────────────────────

export function confirmAndStream(
  body: ConfirmRequest,
  onEvent: SSEEventCallback,
  onDone?: () => void,
  onError?: (err: Error) => void
): { abort: () => void } {
  return createSSEClient('/im/web/message/confirm', body, {
    onEvent,
    onDone,
    onError,
  })
}

// ── Fetch message history ──────────────────────────────────────────────

export function getHistory(sessionId: string): Promise<HistoryResponse> {
  return api.get<HistoryResponse>('/im/web/history', { chat_id: sessionId })
}

// ── Fetch all sessions ─────────────────────────────────────────────────

export function getSessions(): Promise<SessionsResponse> {
  return api.get<SessionsResponse>('/im/web/sessions')
}

// ── Delete a session ─────────────────────────────────────────────────

export function deleteSession(sessionId: string): Promise<{ success: boolean }> {
  return api.delete<{ success: boolean }>(`/im/web/session/${sessionId}`)
}

// ── WebSocket-based messaging ────────────────────────────────────────

let wsClient: AthenaWSClient | null = null
let wsEventHandlers = new Map<string, SSEEventCallback>()
let wsDoneHandlers = new Map<string, () => void>()
let wsErrorHandlers = new Map<string, (err: Error) => void>()

function getWSClient(): AthenaWSClient {
  if (!wsClient) {
    wsClient = createWSClient('/api/v1/ws/im/web', {
      onEvent: (eventType, data) => {
        wsEventHandlers.forEach((handler) => handler(eventType, data))
      },
      onDone: () => {
        wsDoneHandlers.forEach((handler) => handler())
      },
      onError: (err) => {
        wsErrorHandlers.forEach((handler) => handler(err))
      },
      autoReconnect: true,
    })
    wsClient.connect()
  }
  return wsClient
}

export function sendMessageWS(
  body: MessageRequest,
  onEvent: SSEEventCallback,
  onDone?: () => void,
  onError?: (err: Error) => void
): { abort: () => void } {
  const key = `msg_${Date.now()}`
  wsEventHandlers.set(key, onEvent)
  if (onDone) wsDoneHandlers.set(key, onDone)
  if (onError) wsErrorHandlers.set(key, onError)

  const client = getWSClient()
  client.sendMessage(body.content, body.chat_id, body.attachments)

  return {
    abort: () => {
      wsEventHandlers.delete(key)
      wsDoneHandlers.delete(key)
      wsErrorHandlers.delete(key)
    },
  }
}

export function confirmAndStreamWS(
  body: ConfirmRequest,
  onEvent: SSEEventCallback,
  onDone?: () => void,
  onError?: (err: Error) => void
): { abort: () => void } {
  const key = `confirm_${Date.now()}`
  wsEventHandlers.set(key, onEvent)
  if (onDone) wsDoneHandlers.set(key, onDone)
  if (onError) wsErrorHandlers.set(key, onError)

  const client = getWSClient()
  client.sendConfirm(body.chat_id, body.approved)

  return {
    abort: () => {
      wsEventHandlers.delete(key)
      wsDoneHandlers.delete(key)
      wsErrorHandlers.delete(key)
    },
  }
}

export function disconnectWS(): void {
  wsClient?.disconnect()
  wsClient = null
  wsEventHandlers.clear()
  wsDoneHandlers.clear()
  wsErrorHandlers.clear()
}
