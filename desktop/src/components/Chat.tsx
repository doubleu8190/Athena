import { useState, useEffect, useRef, useCallback } from "react"
import { Send, Square, Loader2, Sparkles, ChevronDown, ChevronRight, ListChecks, Clock, CheckCircle } from "lucide-react"
import { MessageBubble } from "./MessageBubble"
import { StepCard } from "./StepCard"
import { ApprovalDialog } from "./ApprovalDialog"
import { useChatStore } from "../store/chatStore"
import { apiClient } from "../api/client"
import { ClientEventType } from "../types/events"
import type { ApprovalRequest, Message } from "../types"

interface ChatProps {
  sendEvent: (type: string, data?: Record<string, unknown>) => void
}

function Chat({ sendEvent }: ChatProps) {
  const {
    messages,
    steps,
    activeSessionId,
    agentStatus,
    pendingApprovals,
    thinking,
    error,
    addMessage,
    setMessages,
    setSteps,
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
  const [showSteps, setShowSteps] = useState(false)
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
      // 同时加载 messages 和 steps
      const [history, sessionSteps] = await Promise.all([
        apiClient.getMessages(sessionId),
        apiClient.getSteps(sessionId),
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
      }
    } catch {
      if (isCancelled?.()) return
      if (loadingSessionIdRef.current === sessionId) {
        clearMessages()
        clearSteps()
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

    // 通过 WebSocket 发送用户命令
    sendEvent(ClientEventType.USER_COMMAND, {
      message: text,
      session_id: activeSessionId,
    })

    setIsSending(false)
  }, [input, activeSessionId, isSending, addMessage, sendEvent])

  const handleStop = useCallback(() => {
    if (!activeSessionId) return
    sendEvent(ClientEventType.SESSION_STOP, {})
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
  // We iterate through messages and track phase transitions
  type Phase = "request" | "processing" | "response"
  interface PhaseGroup {
    phase: Phase
    messages: Message[]
  }

  const phaseGroups: PhaseGroup[] = []
  let currentPhase: Phase | null = null
  for (const msg of messages) {
    let msgPhase: Phase
    if (msg.role === "user") {
      msgPhase = "request"
    } else if (msg.role === "tool") {
      msgPhase = "processing"
    } else {
      // assistant
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
    <div className="flex-1 flex flex-col h-full min-w-0">
      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto">
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

                return (
                  <div key={gi}>
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
                            {/* Show step info inline for tool messages */}
                            {message.role === "tool" && !!message.metadata?.step_number && (
                              <div className="flex items-center gap-2 text-xs text-athena-muted ml-11 mb-2">
                                <span className="font-mono">
                                  Step {String(message.metadata.step_number)}
                                  {!!message.metadata.duration_ms && (
                                    <> · Tool Execution · {formatDuration(Number(message.metadata.duration_ms))}</>
                                  )}
                                </span>
                                {isConversationCompleted && (
                                  <span className="text-athena-success">→ completed</span>
                                )}
                              </div>
                            )}
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
                            <div className="mt-2 text-sm text-athena-text/80 font-mono whitespace-pre-wrap max-h-48 overflow-hidden">
                              {thinking.content}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </>
              )}

              {/* Execution Trace */}
              {!isLoadingHistory && steps.length > 0 && (
                <>
                  <div className="phase-divider">
                    <span className="phase-label phase-trace">
                      <span className="dot"></span>
                      EXECUTION TRACE
                    </span>
                    <span className="text-xs text-athena-muted font-mono">
                      {steps.length} step{steps.length !== 1 ? "s" : ""}
                      {isConversationCompleted && (
                        <> · total {formatTotalDuration(steps)}</>
                      )}
                    </span>
                  </div>

                  <div className="mb-4">
                    {/* Collapsed view for active, expandable for both */}
                    <button
                      onClick={() => setShowSteps(!showSteps)}
                      className="flex items-center gap-2 w-full px-3 py-2 rounded-lg bg-athena-surface border border-athena-border hover:bg-athena-bg transition-colors text-sm"
                    >
                      <ListChecks className="w-4 h-4 text-athena-muted" />
                      <span className="font-medium text-athena-text">Execution Steps</span>
                      <span className="text-xs text-athena-muted ml-1">({steps.length})</span>
                      <span className="ml-auto text-xs text-athena-muted">
                        {isConversationCompleted ? "Show details" : showSteps ? "Hide" : "Show details"}
                      </span>
                      {showSteps ? (
                        <ChevronDown className="w-4 h-4 text-athena-muted" />
                      ) : (
                        <ChevronRight className="w-4 h-4 text-athena-muted" />
                      )}
                    </button>
                    {showSteps && (
                      <div className="mt-2 space-y-2">
                        {steps.map((step) => (
                          <StepCard key={step.id} step={step} compact={false} />
                        ))}
                      </div>
                    )}
                    {/* For completed conversations, also show expanded trace by default */}
                    {isConversationCompleted && !showSteps && (
                      <div className="mt-2 space-y-2">
                        {steps.map((step) => (
                          <StepCard key={step.id} step={step} compact />
                        ))}
                      </div>
                    )}
                  </div>
                </>
              )}
            </>
          )}

          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Input Area */}
      <div className="border-t border-athena-border bg-athena-surface p-4">
        <div className="max-w-3xl mx-auto">
          <div className="flex items-end gap-2">
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
                className="w-full bg-athena-bg border border-athena-border rounded-lg px-4 py-3 pr-12 text-athena-text placeholder:text-athena-muted focus:outline-none focus:border-athena-accent transition-colors resize-none leading-5"
                style={{ maxHeight: "200px", minHeight: "48px" }}
              />
            </div>
            {isAgentActive ? (
              <button
                onClick={handleStop}
                className="inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed bg-athena-danger text-white hover:opacity-90 flex-shrink-0 h-[48px] w-[48px]"
                title="Stop"
              >
                <Square className="w-4 h-4" />
              </button>
            ) : (
              <button
                onClick={handleSend}
                disabled={!input.trim() || !activeSessionId || isSending}
                className="inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed bg-athena-accent text-white hover:bg-athena-accent-hover flex-shrink-0 h-[48px] w-[48px]"
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

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function formatTotalDuration(steps: { duration_ms: number }[]): string {
  const total = steps.reduce((sum, s) => sum + (s.duration_ms || 0), 0)
  return formatDuration(total)
}
