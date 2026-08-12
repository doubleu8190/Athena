import { useState, useEffect, useRef, useCallback } from "react"
import { Send, Square, Loader2, Sparkles, Clock, CheckCircle, PanelRight } from "lucide-react"
import { MessageBubble } from "./MessageBubble"
import { ApprovalDialog } from "./ApprovalDialog"
import { ActivityPanel } from "./ActivityPanel"
import { useChatStore } from "../store/chatStore"
import { apiClient } from "../api/client"
import { ClientEventType } from "../types/events"
import type { ApprovalRequest, Message, ToolCall } from "../types"

interface ChatProps {
  sendEvent: (type: string, data?: Record<string, unknown>) => boolean
}

function Chat({ sendEvent }: ChatProps) {
  const {
    messages,
    steps,
    toolCalls,
    activeSessionId,
    agentStatus,
    pendingApprovals,
    thinking,
    error,
    addMessage,
    removeMessage,
    setMessages,
    setSteps,
    setToolCalls,
    clearMessages,
    clearSteps,
    clearToolCalls,
    clearApprovals,
    setAgentStatus,
    clearThinking,
    setError,
    clearError,
  } = useChatStore()

  const [input, setInput] = useState("")
  const [isSending, setIsSending] = useState(false)
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [showActivity, setShowActivity] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const loadingSessionIdRef = useRef<string | null>(null)

  // 加载会话历史消息 — 切换 session 时完全重置状态
  useEffect(() => {
    let cancelled = false

    if (activeSessionId) {
      // 先完全清除上一个 session 的所有残留状态
      clearMessages()
      clearSteps()
      clearToolCalls()
      clearApprovals()
      clearThinking()
      setAgentStatus("idle")
      clearError()
      // 再加载新 session 的历史
      loadingSessionIdRef.current = activeSessionId
      setIsLoadingHistory(true)
      loadHistory(activeSessionId, () => cancelled)
    } else {
      clearMessages()
      clearSteps()
      clearToolCalls()
      clearApprovals()
      clearThinking()
      setAgentStatus("idle")
      clearError()
    }

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId])

  const loadHistory = async (sessionId: string, isCancelled?: () => boolean) => {
    try {
      // 同时加载 messages、steps 与 tool_calls，确保 Activity 面板完整
      // （此前缺 tool_calls，历史会话的工具调用在面板上一片空白）
      const [history, sessionSteps, sessionToolCalls] = await Promise.all([
        apiClient.getMessages(sessionId),
        apiClient.getSteps(sessionId),
        apiClient.getToolCalls(sessionId) as Promise<RawToolCallRecord[]>,
      ])
      // StrictMode 双调用或快速切换 session 时取消应用结果
      if (isCancelled?.()) return
      // 防止快速切换 session 导致的竞态：只在当前 session 仍匹配时应用结果
      if (loadingSessionIdRef.current === sessionId) {
        if (history.length > 0) {
          setMessages(history)
        } else {
          clearMessages()
        }
        if (sessionSteps.length > 0) {
          setSteps(sessionSteps)
        } else {
          clearSteps()
        }
        if (sessionToolCalls.length > 0) {
          setToolCalls(sessionToolCalls.map(normalizeToolCall))
        } else {
          clearToolCalls()
        }
      }
    } catch {
      if (isCancelled?.()) return
      if (loadingSessionIdRef.current === sessionId) {
        clearMessages()
        clearSteps()
        clearToolCalls()
        setError("Failed to load session history. Please try again.")
      }
    } finally {
      if (!isCancelled?.() && loadingSessionIdRef.current === sessionId) {
        setIsLoadingHistory(false)
      }
    }
  }

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages, thinking])

  // 自动调整 textarea 高度
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = "auto"
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [input])

  const handleSend = useCallback(async () => {
    const text = input.trim()
    if (!text || !activeSessionId || isSending) return

    setIsSending(true)
    setInput("")

    // 添加用户消息到 UI
    const userMsg = {
      id: crypto.randomUUID(),
      role: "user" as const,
      content: text,
      timestamp: new Date().toISOString(),
      session_id: activeSessionId,
    }
    addMessage(userMsg)

    // 乐观反馈：立即进入 running，不等后端首事件（STREAM_START / LLM_CALL_START）。
    // 后端在 LLM 调用前有记忆检索、历史加载、提示词构建等处理窗口，若 UI 保持
    // idle，用户会感觉"发送完消息后没有任何反应"。置为 running 后处理中指示器
    // 与停止按钮立刻出现，消息送达的确定性也随即传达。
    setAgentStatus("running")
    clearThinking()

    // 通过 WebSocket 发送用户命令
    const sent = sendEvent(ClientEventType.USER_COMMAND, {
      message: text,
      session_id: activeSessionId,
    })

    if (!sent) {
      // WebSocket 未连接：消息实际未送达，回滚 UI 并把输入文本还给用户
      setAgentStatus("idle")
      setError("Connection lost. Your message was not sent — please try again.")
      removeMessage(userMsg.id)
      setInput(text)
    }

    setIsSending(false)
  }, [
    input,
    activeSessionId,
    isSending,
    addMessage,
    removeMessage,
    setAgentStatus,
    setError,
    clearThinking,
    sendEvent,
  ])

  const handleStop = useCallback(() => {
    if (!activeSessionId) return
    // 单一全局连接下后端从 data.session_id 取会话，不再依赖 URL 路径
    sendEvent(ClientEventType.SESSION_STOP, { session_id: activeSessionId })
  }, [activeSessionId, sendEvent])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleApproval = useCallback(
    (approval: ApprovalRequest, action: "allow" | "deny") => {
      sendEvent(ClientEventType.APPROVAL_RESPONSE, {
        approval_id: approval.approval_id,
        action,
      })
    },
    [sendEvent],
  )

  const isAgentActive =
    agentStatus === "thinking" ||
    agentStatus === "running" ||
    agentStatus === "waiting_approval"

  // Determine if this session's conversation is completed (has assistant response and agent is idle)
  const isConversationCompleted =
    !isAgentActive &&
    !thinking?.active &&
    messages.some((m) => m.role === "assistant")

  // 会话完成总结（原 EXECUTION TRACE 的聚合信息）
  const failedStepCount = steps.filter((s) => s.status === "failed").length

  // Format conversation time range
  const conversationTime = (() => {
    if (messages.length === 0) return null
    const first = messages[0]
    const last = messages[messages.length - 1]
    const start = formatTime(first.timestamp)
    if (isConversationCompleted && first.timestamp !== last.timestamp) {
      const end = formatTime(last.timestamp)
      const startMs = new Date(first.timestamp).getTime()
      const endMs = new Date(last.timestamp).getTime()
      const diffMin = Math.round((endMs - startMs) / 60000)
      return { start, end, duration: diffMin > 0 ? `${diffMin} min` : null }
    }
    return { start, end: null, duration: null }
  })()

  // Group messages into phases: user request → processing → response
  // assistant 消息是否属于"处理中"不再只看 role —— 模型会在工具调用中途输出文本
  // （如「我来帮你完成…」）并发起纯工具调用回合（content 为空但 tool_calls 有值），
  // 这些都应归入 AGENT PROCESSING；只有真正的最终答复才标为 AGENT RESPONSE。
  type Phase = "request" | "processing" | "response"
  interface PhaseGroup {
    phase: Phase
    messages: Message[]
  }

  const phaseGroups: PhaseGroup[] = []
  let currentPhase: Phase | null = null
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i]
    let msgPhase: Phase
    if (msg.role === "user") {
      msgPhase = "request"
    } else if (msg.role === "tool") {
      msgPhase = "processing"
    } else if (msg.role === "assistant") {
      const hasContent = !!msg.content.trim()
      const hasToolCalls = (msg.tool_calls?.length ?? 0) > 0
      const nextIsTool = messages[i + 1]?.role === "tool"
      // 携带工具调用 / 紧随其后的消息是工具结果 → 处理中；真正的最终答复 → response
      if (hasToolCalls || nextIsTool) {
        msgPhase = "processing"
      } else if (hasContent) {
        msgPhase = "response"
      } else {
        continue // 无内容且无工具调用，跳过空气泡
      }
    } else {
      // system 及其它
      msgPhase = "response"
    }
    if (msgPhase !== currentPhase) {
      phaseGroups.push({ phase: msgPhase, messages: [msg] })
      currentPhase = msgPhase
    } else {
      phaseGroups[phaseGroups.length - 1].messages.push(msg)
    }
  }

  // Track whether we've already rendered the processing-group opening
  // to properly wrap tool messages and thinking in the timeline
  const hasProcessingPhase = phaseGroups.some((g) => g.phase === "processing")

  return (
    <div className="flex-1 flex flex-row min-w-0 min-h-0">
      <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto min-h-0">
        {/* Activity 面板开关(固定悬浮于消息区右上角) */}
        {(messages.length > 0 || isAgentActive) && (
          <div className="absolute top-3 right-4 z-10">
            <button
              type="button"
              onClick={() => setShowActivity((v) => !v)}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium border transition-colors ${
                showActivity
                  ? "bg-athena-accent/10 border-athena-accent/40 text-athena-accent"
                  : "bg-athena-surface border-athena-border text-athena-muted hover:text-athena-text hover:bg-athena-bg"
              }`}
              title="Toggle activity panel"
            >
              <PanelRight className="w-3.5 h-3.5" />
              Activity
            </button>
          </div>
        )}
        <div className="max-w-3xl mx-auto px-4 py-6">
          {/* Loading History Indicator */}
          {isLoadingHistory && (
            <div className="text-center py-16 text-athena-muted">
              <Loader2 className="w-8 h-8 mx-auto mb-4 text-athena-accent animate-spin" />
              <p className="text-sm">Loading conversation history…</p>
            </div>
          )}

          {/* Empty State */}
          {!isLoadingHistory && messages.length === 0 && !thinking && !isAgentActive && (
            <div className="text-center py-16 text-athena-muted">
              <Sparkles className="w-12 h-12 mx-auto mb-4 text-athena-accent" />
              <p className="text-lg font-medium mb-2">How can Athena help you?</p>
              <p className="text-sm">
                Start a new conversation to begin interacting with your AI agent.
              </p>
            </div>
          )}

          {/* Error State */}
          {!isLoadingHistory && messages.length === 0 && error && !thinking && !isAgentActive && (
            <div className="text-center py-16">
              <p className="text-sm text-athena-danger mb-2">{error}</p>
              <p className="text-xs text-athena-muted">
                Try selecting the session again, or create a new one.
              </p>
            </div>
          )}

          {/* Conversation Header Pill */}
          {!isLoadingHistory && messages.length > 0 && conversationTime && (
            <div className="flex justify-center py-1 mb-4">
              <div className="conversation-header-pill bg-athena-surface/80 border border-athena-border">
                {isConversationCompleted ? (
                  <CheckCircle className="w-3 h-3 text-athena-success" />
                ) : (
                  <Clock className="w-3 h-3 text-athena-accent" />
                )}
                <span>
                  {isConversationCompleted ? "Conversation completed" : "Conversation started"}
                </span>
                <span className="text-athena-border">·</span>
                <span>
                  {conversationTime.end
                    ? `${conversationTime.start} — ${conversationTime.end}`
                    : conversationTime.start}
                </span>
                {conversationTime.duration && (
                  <>
                    <span className="text-athena-border">·</span>
                    <span>{conversationTime.duration}</span>
                  </>
                )}
              </div>
            </div>
          )}

          {/* Phased Message Groups */}
          {!isLoadingHistory && messages.length > 0 && (
            <>
              {phaseGroups.map((group, gi) => {
                const isFirstGroup = gi === 0
                const isLastGroup = gi === phaseGroups.length - 1

                // 跨天判断：本组首条消息与上组末条消息比较日期键。
                // 首组恒显示日期，让会话起始的日期可见。
                const prevLast =
                  gi > 0
                    ? phaseGroups[gi - 1].messages[
                        phaseGroups[gi - 1].messages.length - 1
                      ]
                    : null
                const showDateDivider =
                  isFirstGroup ||
                  (prevLast !== null &&
                    dateKey(group.messages[0].timestamp) !==
                      dateKey(prevLast.timestamp))

                return (
                  <div key={gi}>
                    {/* 跨天日期分隔条（今天/昨天/具体日期） */}
                    {showDateDivider && (
                      <div className="flex justify-center py-1">
                        <span className="inline-flex items-center px-3 py-1 rounded-full bg-athena-surface/80 border border-athena-border text-xs text-athena-muted">
                          {formatDateLabel(group.messages[0].timestamp)}
                        </span>
                      </div>
                    )}
                    {/* Phase Divider */}
                    <div className="phase-divider">
                      <span className={`phase-label phase-${group.phase === "request" ? "request" : group.phase === "processing" ? "processing" : "response"}`}>
                        <span className="dot"></span>
                        {group.phase === "request" ? "USER REQUEST" : group.phase === "processing" ? "AGENT PROCESSING" : "AGENT RESPONSE"}
                      </span>
                      {group.messages[0] && (
                        <span className="text-xs text-athena-muted font-mono">
                          {formatTime(group.messages[0].timestamp)}
                        </span>
                      )}
                    </div>

                    {/* USER REQUEST phase — render user message */}
                    {group.phase === "request" && (
                      <div className={isFirstGroup ? "animate-fade-in" : ""}>
                        {group.messages.map((message) => (
                          <MessageBubble key={message.id} message={message} />
                        ))}
                      </div>
                    )}

                    {/* AGENT PROCESSING phase — render inside processing-group timeline */}
                    {group.phase === "processing" && (
                      <div className={`processing-group py-2 ${isConversationCompleted ? "task-completed" : ""}`}>
                        {group.messages.map((message, mi) => (
                          <div key={message.id} className="timeline-node tool-node">
                            <MessageBubble message={message} />
                            {mi < group.messages.length - 1 && <div className="mb-3" />}
                          </div>
                        ))}
                      </div>
                    )}

                    {/* AGENT RESPONSE phase — render assistant message */}
                    {group.phase === "response" && (
                      <div className={isLastGroup && !isAgentActive ? "animate-fade-in" : ""}>
                        {group.messages.map((message) => (
                          <MessageBubble key={message.id} message={message} />
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}

              {/* Active Thinking / Processing indicator — inside a processing-group */}
              {!isLoadingHistory && (thinking?.active || (isAgentActive && !thinking)) && (
                <>
                  {/* Only add processing divider if we don't already have a processing phase */}
                  {!hasProcessingPhase && (
                    <div className="phase-divider">
                      <span className="phase-label phase-processing">
                        <span className="dot"></span>
                        AGENT PROCESSING
                      </span>
                    </div>
                  )}
                  <div className={`processing-group py-2 ${!hasProcessingPhase ? "" : ""}`}>
                    <div className="timeline-node thinking-node">
                      <div className="flex gap-3">
                        <div className="w-8 h-8 rounded-full flex items-center justify-center bg-athena-surface border border-athena-border flex-shrink-0">
                          <Loader2 className="w-4 h-4 text-athena-accent animate-spin" />
                        </div>
                        <div className="card px-4 py-3 flex-1">
                          <div className="flex items-center gap-2 text-sm">
                            <span className="font-medium text-athena-text">
                              {thinking?.active ? "Thinking" : agentStatus === "waiting_approval" ? "Waiting for Approval" : "Processing"}
                            </span>
                            <span className="text-xs text-athena-muted ml-auto">
                              {agentStatus === "waiting_approval" ? "Blocked" : "Running"}
                            </span>
                          </div>
                          <div className="thinking-indicator mt-1">
                            <span></span>
                            <span></span>
                            <span></span>
                          </div>
                          {thinking?.content && (
                            <details className="mt-2" open>
                              <summary className="cursor-pointer text-xs text-athena-muted hover:text-athena-text select-none">
                                Thinking details
                              </summary>
                              <div className="mt-2 text-sm text-athena-text/80 font-mono whitespace-pre-wrap max-h-48 overflow-y-auto">
                                {thinking.content}
                              </div>
                            </details>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </>
              )}

              {/* 会话完成总结 — 原 EXECUTION TRACE 列表移入右侧 Activity 面板，
                  聊天流仅保留一行聚合信息，避免随任务增长而冗长 */}
              {isConversationCompleted && steps.length > 0 && (
                <div className="flex justify-center pt-3">
                  <span className="text-xs text-athena-muted flex items-center gap-1.5">
                    <CheckCircle className="w-3.5 h-3.5 text-athena-success" />
                    Conversation completed · {steps.length} step{steps.length !== 1 ? "s" : ""}
                    · total {formatTotalDuration(steps)}
                    {failedStepCount > 0 && <> · {failedStepCount} failed</>}
                  </span>
                </div>
              )}
            </>
          )}

          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Input Area */}
      <div className="border-t border-athena-border bg-athena-surface p-4">
        <div className="max-w-3xl mx-auto">
          <div className="flex items-stretch gap-2">
            <div className="flex-1 relative">
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder={
                  activeSessionId
                    ? "Send a message to Athena… (Enter to send, Shift+Enter for newline)"
                    : "Select or create a session to start chatting"
                }
                disabled={!activeSessionId}
                rows={1}
                className="block w-full bg-athena-bg border border-athena-border rounded-lg px-4 py-3 pr-12 text-athena-text placeholder:text-athena-muted focus:outline-none focus:border-athena-accent transition-colors resize-none leading-5"
                style={{ maxHeight: "200px", minHeight: "48px" }}
              />
            </div>
            {isAgentActive ? (
              <button
                onClick={handleStop}
                className="inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed bg-athena-danger text-white hover:opacity-90 flex-shrink-0 min-h-[48px] w-[48px]"
                title="Stop"
              >
                <Square className="w-4 h-4" />
              </button>
            ) : (
              <button
                onClick={handleSend}
                disabled={!input.trim() || !activeSessionId || isSending}
                className="inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed bg-athena-accent text-white hover:bg-athena-accent-hover flex-shrink-0 min-h-[48px] w-[48px]"
                title="Send"
              >
                <Send className="w-4 h-4" />
              </button>
            )}
          </div>
          <p className="text-xs text-athena-muted mt-2 text-center">
            Athena may produce incorrect information. Verify important details.
          </p>
        </div>
      </div>
      </div>

      {/* Activity / Workbench 面板(双视图) */}
      {showActivity && (
        <ActivityPanel
          messages={messages}
          toolCalls={toolCalls}
          steps={steps}
          onClose={() => setShowActivity(false)}
        />
      )}

      {/* Approval Dialogs */}
      {pendingApprovals.map((approval) => (
        <ApprovalDialog
          key={approval.approval_id}
          request={approval}
          onApprove={() => handleApproval(approval, "allow")}
          onDeny={() => handleApproval(approval, "deny")}
          onCancel={() => handleApproval(approval, "deny")}
        />
      ))}
    </div>
  )
}

export default Chat

function formatTime(isoString: string): string {
  try {
    const date = new Date(isoString)
    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    })
  } catch {
    return ""
  }
}

/** 日期键（用于跨天判断），形如 "2026-8-12". */
function dateKey(isoString: string): string {
  const d = new Date(isoString)
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

/** 日期分隔条文案：今天 / 昨天 / M月D日 / YYYY年M月D日（非当年带年份）. */
function formatDateLabel(isoString: string): string {
  try {
    const date = new Date(isoString)
    const today = new Date()
    const yesterday = new Date()
    yesterday.setDate(today.getDate() - 1)
    if (dateKey(isoString) === dateKey(today.toISOString())) return "今天"
    if (dateKey(isoString) === dateKey(yesterday.toISOString())) return "昨天"
    const opts: Intl.DateTimeFormatOptions =
      date.getFullYear() === today.getFullYear()
        ? { month: "long", day: "numeric" }
        : { year: "numeric", month: "long", day: "numeric" }
    return date.toLocaleDateString("zh-CN", opts)
  } catch {
    return ""
  }
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function formatTotalDuration(steps: { duration_ms: number }[]): string {
  const total = steps.reduce((sum, s) => sum + (s.duration_ms || 0), 0)
  return formatDuration(total)
}

/** 后端 ToolCallRecord 的原始字段（与前端 ToolCall 命名不同） */
interface RawToolCallRecord {
  id: string
  session_id: string
  step_id: string
  tool_name: string
  arguments: Record<string, unknown>
  raw_output?: string | null
  status: ToolCall["status"]
  started_at: string
  completed_at?: string | null
  duration_ms?: number
  error_message?: string | null
  error_stack?: string | null
}

/** ToolCallRecord → 前端 ToolCall：对齐字段名并补默认 risk_level.
 *  run_id 未存于工具记录，Activity 面板通过 step_id → steps.run_id 归组。 */
function normalizeToolCall(r: RawToolCallRecord): ToolCall {
  return {
    id: r.id,
    tool_name: r.tool_name,
    arguments: r.arguments ?? {},
    status: r.status,
    started_at: r.started_at,
    completed_at: r.completed_at ?? undefined,
    duration_ms: r.duration_ms,
    output: r.raw_output ?? undefined,
    error: r.error_message ?? undefined,
    risk_level: "medium",
    step_id: r.step_id,
  }
}
