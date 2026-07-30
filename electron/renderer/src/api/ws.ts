/**
 * WebSocket client for Athena's real-time agent communication.
 *
 * Provides bidirectional communication for message sending, confirmation,
 * and real-time event streaming. Falls back to SSE if WebSocket is
 * unavailable.
 */

export type WSMessageType = 'message' | 'confirm' | 'ping'

export interface WSMessage {
  type: WSMessageType
  content?: string
  chat_id?: string
  approved?: boolean
  attachments?: Record<string, unknown>[]
}

export interface WSEvent {
  type: string
  data: Record<string, unknown>
}

export type WSEventCallback = (eventType: string, data: Record<string, unknown>) => void
export type WSDoneCallback = () => void
export type WSErrorCallback = (error: Error) => void

export interface WSClientOptions {
  onEvent: WSEventCallback
  onDone?: WSDoneCallback
  onError?: WSErrorCallback
  autoReconnect?: boolean
}

export class AthenaWSClient {
  private ws: WebSocket | null = null
  private url: string
  private options: WSClientOptions
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private shouldReconnect: boolean = true
  private isActive: boolean = false
  private pendingMessages: WSMessage[] = []

  constructor(url: string, options: WSClientOptions) {
    this.url = url
    this.options = { autoReconnect: true, ...options }
  }

  connect(): void {
    this.shouldReconnect = this.options.autoReconnect ?? true
    this.isActive = true
    this.doConnect()
  }

  private doConnect(): void {
    try {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const host = window.location.host || 'localhost:8000'
      const fullUrl = this.url.startsWith('ws')
        ? this.url
        : `${proto}//${host}${this.url}`

      this.ws = new WebSocket(fullUrl)

      this.ws.onopen = () => {
        this.flushPending()
      }

      this.ws.onmessage = (event) => {
        try {
          const payload: WSEvent = JSON.parse(event.data)
          if (payload.type === 'pong') return
          this.options.onEvent(payload.type, payload.data)
        } catch {
          this.options.onEvent('error', { message: 'Invalid WebSocket message' })
        }
      }

      this.ws.onclose = () => {
        if (this.isActive && this.shouldReconnect) {
          this.scheduleReconnect()
        } else if (!this.isActive) {
          this.options.onDone?.()
        }
      }

      this.ws.onerror = () => {
        const err = new Error('WebSocket connection error')
        this.options.onError?.(err)
      }
    } catch (err) {
      this.options.onError?.(err instanceof Error ? err : new Error(String(err)))
      this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (this.shouldReconnect && this.isActive) {
        this.doConnect()
      }
    }, 2000)
  }

  private flushPending(): void {
    while (this.pendingMessages.length > 0) {
      const msg = this.pendingMessages.shift()!
      this.send(msg)
    }
  }

  send(message: WSMessage): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message))
    } else {
      this.pendingMessages.push(message)
    }
  }

  sendMessage(content: string, chatId: string, attachments?: Record<string, unknown>[]): void {
    this.send({ type: 'message', content, chat_id: chatId, attachments })
  }

  sendConfirm(chatId: string, approved: boolean): void {
    this.send({ type: 'confirm', chat_id: chatId, approved })
  }

  disconnect(): void {
    this.isActive = false
    this.shouldReconnect = false
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.ws?.close()
    this.ws = null
  }

  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }
}

export function createWSClient(
  url: string,
  options: WSClientOptions
): AthenaWSClient {
  return new AthenaWSClient(url, options)
}
