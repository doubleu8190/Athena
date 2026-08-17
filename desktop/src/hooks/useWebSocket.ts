import { useCallback, useEffect, useRef } from "react"
import { EventType, ClientEventType } from "../types/events"
import type {
  WebSocketEvent,
  ApprovalRequest,
  ToolCall,
  Message,
  Step,
  Attachment,
  FileTask,
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
  const hasUserCommandRef = useRef(false)
  // 记录当前应订阅的会话，供 onopen / 重连后补发 SUBSCRIBE
  const sessionIdRef = useRef<string | null>(sessionId)
  const {
    setConnectionStatus,
    setAgentStatus,
    addMessage,
    updateMessage,
    removeMessage,
    addToolCall,
    updateToolCall,
    addStep,
    updateStep,
    addApproval,
    resolveApproval,
    setThinking,
    clearThinking,
    setError,
    clearError,
    upsertAttachment,
    upsertFileTask,
  } = useChatStore()

  const buildWsUrl = useCallback(() => {
    const wsBase = apiBase.replace("http://", "ws://").replace("https://", "wss://")
    // 单一全局连接：应用只建一条 /ws，会话切换靠 SUBSCRIBE 消息，
    // 不再为每个会话单独建连（避免切换会话时的握手抖动与事件丢失窗口）
    return `${wsBase}/ws`
  }, [apiBase])

  // 发送订阅/退订消息（socket 未就绪时静默，onopen 会补发）
  const sendSubscribe = useCallback((sid: string | null) => {
    const ws = wsRef.current
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: ClientEventType.SUBSCRIBE, data: { session_id: sid } }))
    }
  }, [])

  const connect = useCallback(() => {
    // 清理旧连接（仅在重连/换 apiBase 时发生）
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }

    const url = buildWsUrl()
    const ws = new WebSocket(url)
    wsRef.current = ws

    setConnectionStatus("connecting")

    ws.onopen = () => {
      // 旧 socket 的 onopen 晚到（被 disconnect()/新 connect() 替换后）：
      // 忽略，避免误发订阅、误重置重连计数、误覆盖连接状态
      if (wsRef.current !== ws) return
      setConnectionStatus("connected")
      reconnectAttempts.current = 0
      onOpen?.()
      // 连接建立/重连成功后恢复订阅（sessionIdRef 始终是最新目标）
      sendSubscribe(sessionIdRef.current)
    }

    ws.onclose = () => {
      // 仅在"被关闭的 socket 仍是当前 socket"时才视为真实断线：
      // disconnect()/新 connect() 主动关闭的旧 socket，其 onclose 异步晚到，
      // 若不拦截会错误地把状态置为 disconnected，并触发不必要的重连 →
      // 重连又关掉新 socket → 无限重连循环（React StrictMode 双挂载
      // 恰好制造这种旧 socket，是重复 subscribe 的根因）。
      if (wsRef.current !== ws) return
      wsRef.current = null
      setConnectionStatus("disconnected")
      onClose?.()

      // 自动重连（最多 5 次）
      if (reconnectAttempts.current < 5) {
        reconnectAttempts.current++
        const delay = Math.min(1000 * Math.pow(2, reconnectAttempts.current), 30000)
        reconnectTimer.current = setTimeout(() => {
          connect()
        }, delay)
      } else {
        // 重连彻底失败：发送后乐观置为 running 的 run 无法继续，
        // 若保持 running，UI 会永远卡在"处理中"，这里重置回 idle
        setAgentStatus("idle")
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
  [buildWsUrl, onOpen, onClose, onError, sendSubscribe])

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

  // 挂载/卸载 effect：连接生命周期 = 应用生命周期，只建一次
  useEffect(() => {
    connect()
    return () => {
      disconnect()
    }
  }, [connect, disconnect])

  // 页面重新可见时（从休眠/最小化恢复），若连接已断开则自动重连
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        const ws = wsRef.current
        // 连接不存在或已关闭 → 重置重连计数并重新连接
        if (!ws || ws.readyState === WebSocket.CLOSED) {
          reconnectAttempts.current = 0
          connect()
        }
      }
    }
    document.addEventListener("visibilitychange", handleVisibilityChange)
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange)
    }
  }, [connect])

  // 订阅 effect：切换会话只发 SUBSCRIBE，不再重建连接
  useEffect(() => {
    sessionIdRef.current = sessionId
    // 切换 session 时重置所有会话相关状态
    buildingMessageId.current = null
    hasUserCommandRef.current = false
    setAgentStatus("idle")
    clearThinking()
    sendSubscribe(sessionId)
  }, [sessionId, sendSubscribe, setAgentStatus, clearThinking])

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
    (type: string, data: Record<string, unknown> = {}): boolean => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        // 标记用户已发送命令，允许 agent 状态转为 running
        if (type === ClientEventType.USER_COMMAND) {
          hasUserCommandRef.current = true
        }
        wsRef.current.send(JSON.stringify({ type, data }))
        return true
      }
      return false
    },
    [],
  )

  const handleEvent = useCallback(
    (event: WebSocketEvent) => {
      // 过滤不属于当前 session 的事件（防止切换 session 时旧事件残留）
      if (event.session_id && sessionId && event.session_id !== sessionId) {
        return
      }

      const { type, data } = event

      switch (type) {
        case EventType.SESSION_START:
        case EventType.STREAM_START:
          // 仅在用户已发送过命令后才将状态设为 running，
          // 防止仅因 WebSocket 连接建立就错误显示"运行中"
          if (hasUserCommandRef.current) {
            setAgentStatus("running")
          }
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
          // 实时步骤流：后端只把 step 落库、不推送 STEP 事件，
          // 前端从 LLM/TOOL 事件还原 step，使 Activity 面板随执行推进更新
          const stepId = data.step_id as string | undefined
          if (stepId) {
            addStep(
              makeLiveStep(stepId, "llm_call", event.run_id, sessionId),
            )
          }
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

        case EventType.LLM_CALL_END: {
          if (hasUserCommandRef.current) {
            setAgentStatus("running")
          }
          const stepId = data.step_id as string | undefined
          if (stepId) {
            updateStep(stepId, {
              status: data.status === "failed" ? "failed" : "completed",
              duration_ms: (data.duration_ms as number) ?? 0,
              completed_at: new Date().toISOString(),
            })
          }
          const buildingId = buildingMessageId.current
          // 纯工具调用回合（无文本）结束时把工具调用挂到气泡上，
          // 使实时流也能展示工具卡片（与历史视图一致）
          const tc = data.tool_calls as Message["tool_calls"]
          if (buildingId && tc?.length) {
            updateMessage(buildingId, { tool_calls: tc })
          }
          buildingMessageId.current = null
          // status=failed（空响应/异常重试路径）：移除本次调用创建的气泡，
          // 避免残留空气泡；下次 LLM_CALL_START 会创建新气泡
          if (buildingId && data.status === "failed") {
            removeMessage(buildingId)
          }
          clearThinking()
          break
        }

        case EventType.TOOL_CALL_START: {
          const tc: ToolCall = {
            id: (data.tool_call_id as string) ?? crypto.randomUUID(),
            tool_name: data.tool_name as string,
            arguments: (data.arguments as Record<string, unknown>) ?? {},
            status: "running",
            started_at: new Date().toISOString(),
            risk_level: (data.risk_level as "low" | "medium" | "high") ?? "low",
            // 归组键：step_id → steps.run_id；run_id 直接来自事件
            step_id: data.step_id as string | undefined,
            run_id: event.run_id as string | undefined,
          }
          addToolCall(tc)
          // 与 TOOL_CALL_END 配对：为同一 step_id 补一条 tool_execution step
          const stepId = data.step_id as string | undefined
          if (stepId) {
            addStep(
              makeLiveStep(stepId, "tool_execution", event.run_id, sessionId),
            )
          }
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
          // 终态化 tool_execution step：优先用事件自带的 step_id；
          // 兜底：后端旧版本 TOOL_CALL_END 不含 step_id 时，从已记录的
          // ToolCall.step_id（TOOL_CALL_START 时存入）匹配，避免 step 永久 running
          let stepId = data.step_id as string | undefined
          if (!stepId) {
            stepId = useChatStore
              .getState()
              .toolCalls.find((tc) => tc.id === tcId)?.step_id
          }
          if (stepId) {
            updateStep(stepId, {
              status: data.status === "success" ? "completed" : "failed",
              duration_ms: (data.duration_ms as number) ?? 0,
              completed_at: new Date().toISOString(),
              error_message: (data.error as string) ?? undefined,
            })
          }
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

        case EventType.ATTACHMENT_UPDATED:
          upsertAttachment(data as unknown as Attachment)
          break

        case EventType.FILE_TASK_CREATED:
        case EventType.FILE_TASK_PROGRESS:
        case EventType.FILE_TASK_COMPLETED:
        case EventType.FILE_TASK_FAILED:
          upsertFileTask(data as unknown as FileTask)
          break

        case EventType.AGENT_WAITING_FILE:
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
      removeMessage,
      addToolCall,
      updateToolCall,
      addStep,
      updateStep,
      addApproval,
      resolveApproval,
      setError,
      clearError,
      upsertAttachment,
      upsertFileTask,
      sessionId,
    ],
  )

  return {
    sendEvent,
    connectionStatus: useChatStore((s) => s.connectionStatus),
  }
}

/** 从 LLM/TOOL 事件构造一条实时 step（后端只落库不推送 STEP 事件）.
 *  step_number 用当前 store 计数近似；历史重载后以数据库中的真实值替换。 */
function makeLiveStep(
  id: string,
  stepType: Step["step_type"],
  runId: string | undefined,
  sessionId: string | null,
): Step {
  return {
    id,
    session_id: sessionId ?? "",
    run_id: runId ?? "",
    step_number: useChatStore.getState().steps.length + 1,
    step_type: stepType,
    status: "running",
    started_at: new Date().toISOString(),
    duration_ms: 0,
    llm_input_tokens: 0,
    llm_output_tokens: 0,
  }
}
