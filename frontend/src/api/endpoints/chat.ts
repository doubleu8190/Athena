import { api } from '../client'
import { createSSEClient, type SSEEventCallback } from '../sse'
import type { Message } from '../../stores/chatStore'

// ── Types ─────────────────────────────────────────────────────────────

export interface MessageRequest {
  content: string
  session_id: string
  attachments?: Record<string, unknown>[]
}

export interface ConfirmRequest {
  session_id: string
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
  return createSSEClient('/api/v1/im/web/message', body, {
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
  return createSSEClient('/api/v1/im/web/message/confirm', body, {
    onEvent,
    onDone,
    onError,
  })
}

// ── Fetch message history ──────────────────────────────────────────────

export function getHistory(sessionId: string): Promise<HistoryResponse> {
  return api.get<HistoryResponse>('/im/web/history', { session_id: sessionId })
}

// ── Fetch all sessions ─────────────────────────────────────────────────

export function getSessions(): Promise<SessionsResponse> {
  return api.get<SessionsResponse>('/im/web/sessions')
}
