import { useState, useEffect, useRef, useCallback } from "react"
import { Send, Square, Loader2, Sparkles } from "lucide-react"
import { MessageBubble } from "./MessageBubble"
import { ApprovalDialog } from "./ApprovalDialog"
import { useChatStore } from "../store/chatStore"
import { apiClient } from "../api/client"
import { ClientEventType } from "../types/events"
import type { ApprovalRequest } from "../types"

interface ChatProps {
  sendEvent: (type: string, data?: Record<string, unknown>) => void
}

function Chat({ sendEvent }: ChatProps) {
  const {
    messages,
    activeSessionId,
    agentStatus,
    pendingApprovals,
    thinking,
    error,
    addMessage,
    setMessages,
    clearMessages,
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
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const loadingSessionIdRef = useRef<string | null>(null)

  // 加载会话历史消息 — 切换 session 时完全重置状态
  useEffect(() => {
    if (activeSessionId) {
      // 先完全清除上一个 session 的所有残留状态
      clearMessages()
      clearToolCalls()
      clearApprovals()
      clearThinking()
      setAgentStatus("idle")
      clearError()
      // 再加载新 session 的历史
      loadingSessionIdRef.current = activeSessionId
      setIsLoadingHistory(true)
      loadHistory(activeSessionId)
    } else {
      clearMessages()
      clearToolCalls()
      clearApprovals()
      clearThinking()
      setAgentStatus("idle")
      clearError()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId])

  const loadHistory = async (sessionId: string) => {
    try {
      const history = await apiClient.getMessages(sessionId)
      // 防止快速切换 session 导致的竞态：只在当前 session 仍匹配时应用结果
      if (loadingSessionIdRef.current === sessionId) {
        if (history.length > 0) {
          setMessages(history)
        } else {
          clearMessages()
        }
      }
    } catch {
      if (loadingSessionIdRef.current === sessionId) {
        clearMessages()
        setError("Failed to load session history. Please try again.")
      }
    } finally {
      if (loadingSessionIdRef.current === sessionId) {
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

  return (
    <div className="flex-1 flex flex-col h-full min-w-0">
      {/* Messages Area */}
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-4 py-6 space-y-4">
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

          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}

          {/* Thinking Indicator */}
          {!isLoadingHistory && thinking?.active && (
            <div className="flex gap-3">
              <div className="w-8 h-8 rounded-full flex items-center justify-center bg-athena-surface border border-athena-border">
                <Loader2 className="w-4 h-4 text-athena-accent animate-spin" />
              </div>
              <div className="card px-4 py-3 max-w-[85%]">
                <div className="thinking-indicator">
                  <span></span>
                  <span></span>
                  <span></span>
                  <span className="ml-2 text-athena-muted">Thinking…</span>
                </div>
                {thinking.content && (
                  <div className="mt-2 text-sm text-athena-text/80 font-mono whitespace-pre-wrap max-h-48 overflow-hidden">
                    {thinking.content}
                  </div>
                )}
              </div>
            </div>
          )}

          {!isLoadingHistory && isAgentActive && !thinking && (
            <div className="flex gap-3">
              <div className="w-8 h-8 rounded-full flex items-center justify-center bg-athena-surface border border-athena-border">
                <Loader2 className="w-4 h-4 text-athena-accent animate-spin" />
              </div>
              <div className="card px-4 py-3">
                <div className="thinking-indicator">
                  <span></span>
                  <span></span>
                  <span></span>
                  <span className="ml-2">{agentStatus === "waiting_approval" ? "Waiting for approval…" : "Processing…"}</span>
                </div>
              </div>
            </div>
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
                className="textarea pr-12"
                style={{ maxHeight: "200px" }}
              />
            </div>
            {isAgentActive ? (
              <button
                onClick={handleStop}
                className="btn-danger flex-shrink-0"
                title="Stop"
              >
                <Square className="w-4 h-4" />
              </button>
            ) : (
              <button
                onClick={handleSend}
                disabled={!input.trim() || !activeSessionId || isSending}
                className="btn-primary flex-shrink-0"
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
