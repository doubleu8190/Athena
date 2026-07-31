import { useCallback, useEffect, useRef } from "react"
import { EventType, ClientEventType } from "../types/events"
import type {
  WebSocketEvent,
  ApprovalRequest,
  ToolCall,
} from "../types"
import { useChatStore } from "../store/chatStore"

interface UseWebSocketOptions {
  sessionId: string | null
  apiBase: string
  onOpen?: () => void
  onClose?: () => void
  onError?: () => void
}

export function useWebSocket(options: UseWebSocketOptions) {
  const { sessionId, apiBase, onOpen, onClose, onError } = options
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectAttempts = useRef(0)
  const buildingMessageId = useRef<string | null>(null)
  const {
    setConnectionStatus,
    setAgentStatus,
    addMessage,
    updateMessage,
    addToolCall,
    updateToolCall,
    addApproval,
    resolveApproval,
    setThinking,
    clearThinking,
    setError,
    clearError,
  } = useChatStore()

  const buildWsUrl = useCallback(
    (sid: string) => {
      const wsBase = apiBase.replace("http://", "ws://").replace("https://", "wss://")
      return `${wsBase}/ws/${sid}`
    },
    [apiBase],
  )

  const connect = useCallback(
    (sid: string) => {
      // 清理旧连接
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }

      const url = buildWsUrl(sid)
      const ws = new WebSocket(url)
      wsRef.current = ws

      setConnectionStatus("connecting")

      ws.onopen = () => {
        setConnectionStatus("connected")
        reconnectAttempts.current = 0
        onOpen?.()
      }

      ws.onclose = () => {
        setConnectionStatus("disconnected")
        onClose?.()
        wsRef.current = null

        // 自动重连（最多 5 次）
        if (reconnectAttempts.current < 5) {
          reconnectAttempts.current++
          const delay = Math.min(1000 * Math.pow(2, reconnectAttempts.current), 30000)
          reconnectTimer.current = setTimeout(() => {
            if (sessionId === sid) {
              connect(sid)
            }
          }, delay)
        }
      }

      ws.onerror = () => {
        setConnectionStatus("error")
        onError?.()
      }

      ws.onmessage = (event) => {
        try {
          const msg: WebSocketEvent = JSON.parse(event.data)
          handleEvent(msg)
        } catch (e) {
          console.error("Failed to parse WS message:", e)
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [buildWsUrl, onOpen, onClose, onError],
  )

  const disconnect = useCallback(() => {
    if (reconnectTimer.current) {
      clearTimeout(reconnectTimer.current)
      reconnectTimer.current = null
    }
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
  }, [])

  // 监听 sessionId 变化
  useEffect(() => {
    if (sessionId) {
      connect(sessionId)
    } else {
      disconnect()
      setConnectionStatus("disconnected")
    }
    return () => {
      disconnect()
    }
  }, [sessionId, connect, disconnect, setConnectionStatus])

  // 心跳
  useEffect(() => {
    const interval = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(
          JSON.stringify({ type: ClientEventType.PING, data: {} }),
        )
      }
    }, 30000)
    return () => clearInterval(interval)
  }, [])

  const sendEvent = useCallback(
    (type: string, data: Record<string, unknown> = {}) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type, data }))
      }
    },
    [],
  )

  const handleEvent = useCallback(
    (event: WebSocketEvent) => {
      const { type, data } = event

      switch (type) {
        case EventType.SESSION_START:
        case EventType.STREAM_START:
          setAgentStatus("running")
          clearError()
          break

        case EventType.STREAM_END:
        case EventType.SESSION_COMPLETE:
          setAgentStatus("idle")
          clearThinking()
          break

        case EventType.LLM_CALL_START: {
          setAgentStatus("thinking")
          // Create a new assistant message for streaming
          const msgId = crypto.randomUUID()
          buildingMessageId.current = msgId
          addMessage({
            id: msgId,
            role: "assistant",
            content: "",
            timestamp: new Date().toISOString(),
            session_id: sessionId ?? undefined,
          })
          break
        }

        case EventType.LLM_TOKEN: {
          const token = (data.token as string) ?? ""
          if (!token) break

          const msgId = buildingMessageId.current
          if (msgId) {
            // Append token to the building message
            const state = useChatStore.getState()
            const msg = state.messages.find((m) => m.id === msgId)
            if (msg) {
              updateMessage(msgId, { content: msg.content + token })
            }
          } else {
            // Fallback: use thinking buffer
            setThinking({
              active: true,
              content: (useChatStore.getState().thinking?.content ?? "") + token,
              messageId: null,
            })
          }
          break
        }

        case EventType.LLM_CALL_END:
          setAgentStatus("running")
          buildingMessageId.current = null
          clearThinking()
          break

        case EventType.TOOL_CALL_START: {
          const tc: ToolCall = {
            id: (data.tool_call_id as string) ?? crypto.randomUUID(),
            tool_name: data.tool_name as string,
            arguments: (data.arguments as Record<string, unknown>) ?? {},
            status: "running",
            started_at: new Date().toISOString(),
            risk_level: (data.risk_level as "low" | "medium" | "high") ?? "low",
          }
          addToolCall(tc)
          break
        }

        case EventType.TOOL_CALL_END: {
          const tcId = data.tool_call_id as string
          updateToolCall(tcId, {
            status: (data.status as ToolCall["status"]) ?? "success",
            output: data.output as string | undefined,
            error: data.error as string | undefined,
            duration_ms: data.duration_ms as number | undefined,
          })
          break
        }

        case EventType.TOOL_CALL_SKIPPED: {
          const tcId = data.tool_call_id as string
          if (tcId) {
            updateToolCall(tcId, { status: "denied" })
          }
          break
        }

        case EventType.APPROVAL_REQUEST: {
          const req: ApprovalRequest = {
            approval_id: data.approval_id as string,
            tool_name: data.tool_name as string,
            arguments: (data.arguments as Record<string, unknown>) ?? {},
            risk_level: (data.risk_level as ApprovalRequest["risk_level"]) ?? "low",
            timeout: (data.timeout as number) ?? 120,
            description: data.description as string | undefined,
          }
          addApproval(req)
          setAgentStatus("waiting_approval")
          break
        }

        case EventType.APPROVAL_RESULT: {
          const approvalId = data.approval_id as string
          if (approvalId) {
            resolveApproval(approvalId, data.decision as string)
          }
          setAgentStatus("running")
          break
        }

        case EventType.APPROVAL_TIMEOUT: {
          const approvalId = data.approval_id as string
          if (approvalId) {
            resolveApproval(approvalId, "timeout")
          }
          break
        }

        case EventType.ERROR:
          setError(data.error as string ?? "Unknown error")
          setAgentStatus("error")
          break

        case EventType.ERROR_RECOVERED:
          setAgentStatus("running")
          break

        case EventType.BUDGET_EXCEEDED:
          setError("Agent budget exceeded")
          setAgentStatus("idle")
          break

        case EventType.SESSION_INTERRUPTED:
          setAgentStatus("idle")
          break

        case EventType.SESSION_RECOVERED:
          setAgentStatus("running")
          break

        default:
          // 未处理的事件类型暂不处理
          break
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      setAgentStatus,
      setThinking,
      clearThinking,
      addMessage,
      updateMessage,
      addToolCall,
      updateToolCall,
      addApproval,
      resolveApproval,
      setError,
      clearError,
      sessionId,
    ],
  )

  return {
    sendEvent,
    connectionStatus: useChatStore((s) => s.connectionStatus),
  }
}
