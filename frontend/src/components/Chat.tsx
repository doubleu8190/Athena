import { useState, useEffect, useRef, useCallback } from "react"
import { Send, Square, Loader2, Sparkles, Clock, CheckCircle, PanelRight, Paperclip, Files, X, Trash2 } from "lucide-react"
import { MessageBubble } from "./MessageBubble"
import { ApprovalDialog } from "./ApprovalDialog"
import { ActivityPanel } from "./ActivityPanel"
import { useChatStore } from "../store/chatStore"
import { apiClient } from "../api/client"
import { ClientEventType } from "../types/events"
import type { ApprovalRequest, Message, SupportedAttachmentTypes, ToolCallInvocation } from "../types"

interface ChatProps {
  sendEvent: (type: string, data?: Record<string, unknown>) => boolean
}

function isSupportedAttachment(file: File, supportedTypes: SupportedAttachmentTypes): boolean {
  const filename = file.name.toLowerCase()
  return supportedTypes.extensions.some((extension) => filename.endsWith(extension.toLowerCase()))
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
    clearMessages,
    clearSteps,
    clearToolCalls,
    clearApprovals,
    setAgentStatus,
    clearThinking,
    setError,
    clearError,
    attachments,
    setAttachments,
    removeAttachment,
  } = useChatStore()

  const [input, setInput] = useState("")
  const [isSending, setIsSending] = useState(false)
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [showActivity, setShowActivity] = useState(false)
  const [showFiles, setShowFiles] = useState(false)
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  const [isUploading, setIsUploading] = useState(false)
  const [supportedAttachmentTypes, setSupportedAttachmentTypes] = useState<SupportedAttachmentTypes | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const loadingSessionIdRef = useRef<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

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
      setPendingFiles([])
      setSupportedAttachmentTypes(null)
      apiClient.listAttachments(activeSessionId)
        .then((items) => { if (!cancelled) setAttachments(items) })
        .catch(() => { if (!cancelled) setAttachments([]) })
      apiClient.getSupportedAttachmentTypes(activeSessionId)
        .then((types) => { if (!cancelled) setSupportedAttachmentTypes(types) })
        .catch(() => { if (!cancelled) setSupportedAttachmentTypes(null) })
    } else {
      clearMessages()
      clearSteps()
      clearToolCalls()
      clearApprovals()
      clearThinking()
      setAgentStatus("idle")
      clearError()
      setAttachments([])
      setPendingFiles([])
      setSupportedAttachmentTypes(null)
    }

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId])

  const loadHistory = async (sessionId: string, isCancelled?: () => boolean) => {
    try {
      // 运行过程由 SSE Durable Event replay 重建；这里只读取持久消息正文。
      const history = await apiClient.getMessages(sessionId)
      // StrictMode 双调用或快速切换 session 时取消应用结果
      if (isCancelled?.()) return
      // 防止快速切换 session 导致的竞态：只在当前 session 仍匹配时应用结果
      if (loadingSessionIdRef.current === sessionId) {
        if (history.length > 0) {
          setMessages(history)
        } else {
          clearMessages()
        }
        clearSteps()
        clearToolCalls()
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
    if ((!text && pendingFiles.length === 0) || !activeSessionId || isSending || isUploading) return

    setIsSending(true)
    setInput("")

    // 添加用户消息到 UI
    const userMsg = {
      id: crypto.randomUUID(),
      role: "user" as const,
      content: text || "Please process the attached files.",
      timestamp: new Date().toISOString(),
      session_id: activeSessionId,
      attachments: [],
    }
    addMessage(userMsg)

    // 乐观反馈：立即进入 running，不等后端首事件（STREAM_START / LLM_CALL_START）。
    // 后端在 LLM 调用前有记忆检索、历史加载、提示词构建等处理窗口，若 UI 保持
    // idle，用户会感觉"发送完消息后没有任何反应"。置为 running 后处理中指示器
    // 与停止按钮立刻出现，消息送达的确定性也随即传达。
    setAgentStatus("running")
    clearThinking()

    // 只提交命令；执行结果统一通过当前会话的 SSE 流返回，避免重复写入消息。
    const sent = sendEvent(ClientEventType.USER_COMMAND, {
      message: text || "Please process the attached files.",
      session_id: activeSessionId,
      files: pendingFiles,
    })

    if (!sent) {
      // 命令未被接受时回滚乐观写入的用户消息。
      setAgentStatus("idle")
      setError("Connection lost. Your message was not sent — please try again.")
      removeMessage(userMsg.id)
      setInput(text)
    } else {
      setPendingFiles([])
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
    pendingFiles,
    attachments,
    isUploading,
  ])

  const uploadFiles = useCallback(async (files: File[]) => {
    if (!activeSessionId || files.length === 0 || isUploading) return
    if (!supportedAttachmentTypes) {
      setError("Supported file types are still loading. Please try again.")
      if (fileInputRef.current) fileInputRef.current.value = ""
      return
    }
    const unsupportedFiles = files.filter((file) => !isSupportedAttachment(file, supportedAttachmentTypes))
    if (unsupportedFiles.length > 0) {
      setError(`Unsupported file type: ${unsupportedFiles.map((file) => file.name).join(", ")}`)
      if (fileInputRef.current) fileInputRef.current.value = ""
      return
    }
    setIsUploading(true)
    clearError()
    try {
      setPendingFiles((current) => [...current, ...files])
    } catch (err) {
      setError(err instanceof Error ? err.message : "Attachment upload failed")
    } finally {
      setIsUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ""
    }
  }, [activeSessionId, clearError, isUploading, setError, supportedAttachmentTypes])

  const filesFromClipboard = useCallback((clipboardData: DataTransfer): File[] => {
    const fromItems = Array.from(clipboardData.items ?? [])
      .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)

    const source = fromItems.length > 0
      ? fromItems
      : Array.from(clipboardData.files ?? []).filter((file) => file.type.startsWith("image/"))

    return source.map((file, index) => {
      const extension = file.type.split("/")[1]?.replace("jpeg", "jpg") || "png"
      const hasUsableName = file.name && file.name !== "image.png"
      if (hasUsableName) return file
      const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+$/, "")
      return new File([file], `screenshot-${stamp}${index ? `-${index + 1}` : ""}.${extension}`, {
        type: file.type || "image/png",
        lastModified: file.lastModified || Date.now(),
      })
    })
  }, [])

  const handlePasteUpload = useCallback((clipboardData: DataTransfer): boolean => {
    const files = filesFromClipboard(clipboardData)
    if (files.length === 0) return false
    void uploadFiles(files)
    return true
  }, [filesFromClipboard, uploadFiles])

  useEffect(() => {
    const handleDocumentPaste = (event: ClipboardEvent) => {
      if (!activeSessionId || isUploading || !event.clipboardData) return
      const target = event.target
      const targetNode = target instanceof Node ? target : null
      const pastedInsideChat = targetNode !== null && rootRef.current?.contains(targetNode)
      const activeElement = document.activeElement
      const noFocusedElement = activeElement === null || activeElement === document.body
      if (!pastedInsideChat && !noFocusedElement) return
      if (handlePasteUpload(event.clipboardData)) {
        event.preventDefault()
      }
    }

    document.addEventListener("paste", handleDocumentPaste)
    return () => document.removeEventListener("paste", handleDocumentPaste)
  }, [activeSessionId, handlePasteUpload, isUploading])

  const handleDeleteAttachment = useCallback(async (fileId: string) => {
    if (!activeSessionId) return
    await apiClient.deleteAttachment(activeSessionId, fileId)
    removeAttachment(fileId)
  }, [activeSessionId, removeAttachment])

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

  // 只有存在助手答复且 Agent 已回到空闲状态时，才认为本次会话已完成。
  const isConversationCompleted =
    !isAgentActive &&
    !thinking?.active &&
    messages.some((m) => m.role === "assistant")

  // 会话完成总结（原 EXECUTION TRACE 的聚合信息）
  const failedStepCount = steps.filter((s) => s.status === "failed").length

  // 计算当前消息集合覆盖的会话时间范围。
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

  // 将消息分为用户请求、Agent 处理和最终响应三个阶段。
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
      if (!hasContent && hasToolCalls && allToolResultsArrived(msg.tool_calls ?? [], messages)) {
        continue
      }
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

  // 记录是否已渲染处理阶段的起始节点，以便将工具消息和思考状态包裹在同一时间线上。
  const hasProcessingPhase = phaseGroups.some((g) => g.phase === "processing")

  return (
    <div ref={rootRef} className="flex-1 flex flex-row min-w-0 min-h-0">
      <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
      {/* 消息区域 */}
      <div className="flex-1 overflow-y-auto min-h-0">
        {/* Activity 面板开关，固定在消息区右上角 */}
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
              {/* 历史消息加载指示器 */}
          {isLoadingHistory && (
            <div className="text-center py-16 text-athena-muted">
              <Loader2 className="w-8 h-8 mx-auto mb-4 text-athena-accent animate-spin" />
              <p className="text-sm">Loading conversation history…</p>
            </div>
          )}

          {/* 空会话状态 */}
          {!isLoadingHistory && messages.length === 0 && !thinking && !isAgentActive && (
            <div className="text-center py-16 text-athena-muted">
              <Sparkles className="w-12 h-12 mx-auto mb-4 text-athena-accent" />
              <p className="text-lg font-medium mb-2">How can Athena help you?</p>
              <p className="text-sm">
                Start a new conversation to begin interacting with your AI agent.
              </p>
            </div>
          )}

          {/* 错误状态 */}
          {!isLoadingHistory && messages.length === 0 && error && !thinking && !isAgentActive && (
            <div className="text-center py-16">
              <p className="text-sm text-athena-danger mb-2">{error}</p>
              <p className="text-xs text-athena-muted">
                Try selecting the session again, or create a new one.
              </p>
            </div>
          )}

          {/* 会话摘要标题 */}
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

          {/* 分阶段消息组 */}
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
                    {/* 阶段分隔线 */}
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

                    {/* 用户请求阶段 */}
                    {group.phase === "request" && (
                      <div className={isFirstGroup ? "animate-fade-in" : ""}>
                        {group.messages.map((message) => (
                          <MessageBubble key={message.id} message={message} />
                        ))}
                      </div>
                    )}

                    {/* Agent 处理阶段，渲染在处理时间线中 */}
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

                    {/* Agent 响应阶段 */}
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

              {/* 处理阶段内的思考/执行中指示器 */}
              {!isLoadingHistory && (thinking?.active || (isAgentActive && !thinking)) && (
                <>
                  {/* 仅在当前没有处理阶段时添加分隔线 */}
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

      {/* 输入区域 */}
      <div
        className="border-t border-athena-border bg-athena-surface p-4"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault()
          void uploadFiles(Array.from(event.dataTransfer.files))
        }}
      >
        <div className="max-w-3xl mx-auto">
          {pendingFiles.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-2">
              {pendingFiles.map((file, index) => (
                <div key={`${file.name}-${file.lastModified}-${index}`} className="flex items-center gap-2 max-w-full rounded-md border border-athena-border bg-athena-bg px-2 py-1.5 text-xs">
                  <Paperclip className="w-3.5 h-3.5 text-athena-accent flex-shrink-0" />
                  <span className="truncate max-w-[220px]">{file.name}</span>
                  <button
                    type="button"
                    title="Remove from message"
                    onClick={() => setPendingFiles((files) => files.filter((_, fileIndex) => fileIndex !== index))}
                    className="text-athena-muted hover:text-athena-text"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex items-stretch gap-2">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={supportedAttachmentTypes?.extensions.join(",")}
              className="hidden"
              onChange={(event) => void uploadFiles(Array.from(event.target.files ?? []))}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={!activeSessionId || isUploading || !supportedAttachmentTypes}
              title={
                !activeSessionId
                  ? "Select a session to attach files"
                  : supportedAttachmentTypes
                    ? "Attach files"
                    : "Loading supported file types"
              }
              className="inline-flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-lg border border-athena-border bg-athena-bg text-athena-muted hover:text-athena-text disabled:opacity-50"
            >
              {isUploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Paperclip className="w-4 h-4" />}
            </button>
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
                disabled={(!input.trim() && pendingFiles.length === 0) || !activeSessionId || isSending || isUploading}
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
          {activeSessionId && (
            <button
              type="button"
              onClick={() => setShowFiles((value) => !value)}
              className="mt-2 inline-flex items-center gap-1.5 text-xs text-athena-muted hover:text-athena-text"
            >
              <Files className="w-3.5 h-3.5" />
              {attachments.length} file{attachments.length === 1 ? "" : "s"}
            </button>
          )}
          {showFiles && activeSessionId && (
            <div className="mt-2 max-h-56 overflow-y-auto border-t border-athena-border pt-2">
              {attachments.length === 0 ? (
                <div className="py-3 text-center text-xs text-athena-muted">No files in this session</div>
              ) : attachments.map((item) => {
                return (
                  <div key={item.id} className="flex items-center gap-3 py-2 border-b border-athena-border/60 last:border-0">
                    <Files className="w-4 h-4 text-athena-accent flex-shrink-0" />
                    <div className="min-w-0 flex-1 text-left">
                      <div className="truncate text-xs text-athena-text">{item.filename}</div>
                      <div className="text-[11px] text-athena-muted">{item.status}</div>
                    </div>
                    <button type="button" title="Delete" onClick={() => void handleDeleteAttachment(item.id)} className="text-athena-muted hover:text-athena-danger">
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
      </div>

      {/* Activity / Workbench 面板，支持双视图 */}
      {showActivity && (
        <ActivityPanel
          messages={messages}
          toolCalls={toolCalls}
          steps={steps}
          onClose={() => setShowActivity(false)}
        />
      )}

      {/* 审批对话框 */}
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

function allToolResultsArrived(invocations: ToolCallInvocation[], messages: Message[]): boolean {
  return invocations.every((invocation) =>
    messages.some((message) => (
      message.role === "tool" &&
      (message.tool_call_id === invocation.id || message.tool_call_record_id === invocation.id)
    )),
  )
}
