import { useState, useEffect, useRef, useCallback } from "react"
import { Send, Square, Loader2, Sparkles, Clock, PanelRight, PanelLeft, Paperclip, Files, X, Trash2, ChevronDown, ChevronRight } from "lucide-react"
import { MessageBubble } from "./MessageBubble"
import { ApprovalCard } from "./ApprovalDialog"
import { ExecutionContextPanel } from "./ExecutionContextPanel"
import { ExecutionTimeline } from "./ExecutionTimeline"
import { useChatStore } from "../store/chatStore"
import { apiClient } from "../api/client"
import { ClientEventType } from "../types/events"
import type { ApprovalRequest, ExecutionTimelineEntry, Message, SupportedAttachmentTypes } from "../types"

const STOP_REQUESTS_STORAGE_KEY = "athena:pending-stop-requests"

function readPendingStopRequests(): Map<string, { requestId: string; cursor: number }> {
  try {
    const value = localStorage.getItem(STOP_REQUESTS_STORAGE_KEY)
    const parsed = value ? JSON.parse(value) as Record<string, { requestId?: unknown; cursor?: unknown }> : {}
    return new Map(Object.entries(parsed).flatMap(([key, item]) =>
      typeof item?.requestId === "string" && typeof item.cursor === "number"
        ? [[key, { requestId: item.requestId, cursor: item.cursor }]]
        : [],
    ))
  } catch {
    return new Map()
  }
}

function persistPendingStopRequests(
  requests: Map<string, { requestId: string; cursor: number }>,
): void {
  try {
    localStorage.setItem(STOP_REQUESTS_STORAGE_KEY, JSON.stringify(Object.fromEntries(requests)))
  } catch {
    // The current page still retains the request ID if browser storage is unavailable.
  }
}
interface ChatProps {
  sendEvent: (type: string, sessionId: string | null, data?: Record<string, unknown>) => Promise<{ sessionId: string; messageId: string; runId: string; sequence: number; status: string } | null>
  draft: { id: string; projectId: string } | null
  ensureSession: (draftId: string, projectId: string, title?: string) => Promise<string>
  onOpenSessions?: () => void
  demoMode?: boolean
}

type ChatFeedItem =
  | { kind: "message"; id: string; timestamp: string; message: Message; sessionSeq?: number; runSequence?: number }
  | { kind: "event"; id: string; timestamp: string; entry: ExecutionTimelineEntry; sessionSeq?: number; runSequence?: number }
  | { kind: "run_header"; id: string; timestamp: string; runId: string; sessionSeq?: number; runSequence?: number }

function isSupportedAttachment(file: File, supportedTypes: SupportedAttachmentTypes): boolean {
  const filename = file.name.toLowerCase()
  return supportedTypes.extensions.some((extension) => filename.endsWith(extension.toLowerCase()))
}

function _attachmentTitle(files: File[]): string {
  const first = files[0]?.name.trim() || "附件"
  const suffix = files.length > 1 ? `等${files.length}个文件` : ""
  return `分析${first}${suffix}`.slice(0, 120)
}

function Chat({ sendEvent, draft, ensureSession, onOpenSessions, demoMode = false }: ChatProps) {
  const {
    messages,
    sessions,
    orchestrationTasks,
    orchestrationPlans,
    activeSessionId,
    agentStatus,
    connectionStatus,
    pendingApprovals,
    thinking,
    error,
    setMessages,
    addMessage,
    updateSession,
    clearMessages,
    clearSteps,
    clearToolCalls,
    clearOrchestrationTasks,
    clearOrchestrationPlans,
    clearApprovals,
    executionTimeline,
    runTimelineEvents,
    setAgentStatus,
    clearThinking,
    setError,
    clearError,
    attachments,
    suppressRunOutput,
    restoreRunOutput,
    setAttachments,
    upsertAttachment,
    removeAttachment,
  } = useChatStore()

  const [input, setInput] = useState("")
  const [isSending, setIsSending] = useState(false)
  const [stopAllowsSend, setStopAllowsSend] = useState(false)
  const [stopRequestUncertain, setStopRequestUncertain] = useState(false)
  const [hasQueuedRuns, setHasQueuedRuns] = useState(false)
  const [runSequenceState, setRunSequenceState] = useState<Record<string, number>>({})
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [showActivity, setShowActivity] = useState(false)
  const [collapsedRunIds, setCollapsedRunIds] = useState<Set<string>>(() => new Set())
  const [activityNow, setActivityNow] = useState(Date.now())
  const [showFiles, setShowFiles] = useState(false)
  const [selectedAttachmentIds, setSelectedAttachmentIds] = useState<string[]>([])
  const [queuedFiles, setQueuedFiles] = useState<File[]>([])
  const [isUploading, setIsUploading] = useState(false)
  const [supportedAttachmentTypes, setSupportedAttachmentTypes] = useState<SupportedAttachmentTypes | null>(null)
  const messagesContainerRef = useRef<HTMLDivElement>(null)
  // Only follow new content while the user is already reading the tail. Once
  // they scroll up, streaming events must not steal the viewport back.
  const shouldFollowLatestRef = useRef(true)
  const rootRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const loadingSessionIdRef = useRef<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const pendingSendsRef = useRef(new Map<string, { fingerprint: string; requestId: string }>())
  const stopRequestsRef = useRef(readPendingStopRequests())
  const runStateRefreshGenerationRef = useRef(0)
  const runStateRequestSequenceRef = useRef(0)
  const runStateAppliedSequenceRef = useRef(0)
  const composerDraftsRef = useRef<Map<string, string>>(new Map())
  const queuedFilesByTargetRef = useRef<Map<string, File[]>>(new Map())
  const latestInputRef = useRef(input)
  const composerKey = activeSessionId ?? draft?.id ?? "none"
  const composerKeyRef = useRef(composerKey)
  composerKeyRef.current = composerKey
  const previousComposerKeyRef = useRef(composerKey)
  const hasPendingSelectedAttachments = selectedAttachmentIds.some((id) => {
    const item = attachments.find((attachment) => attachment.id === id)
    return !item || item.status !== "ready"
  })
  const activeRunId = sessions.find((session) => session.id === activeSessionId)?.active_run_id ?? null
  const isAgentActive = Boolean(activeRunId || hasQueuedRuns)
    || agentStatus === "queued"
    || agentStatus === "thinking"
    || agentStatus === "running"
    || agentStatus === "cancelling"
    || agentStatus === "waiting_approval"
  const canSend = (input.trim().length > 0 || selectedAttachmentIds.length > 0)
    && !hasPendingSelectedAttachments
    && !isSending
    && !isUploading
    && (!isAgentActive || stopAllowsSend)
    && Boolean(activeSessionId || draft)
  const hasPendingAttachmentProcessing = attachments.some(
    (item) => item.status === "uploaded" || item.status === "processing",
  )

  useEffect(() => {
    if (!activeSessionId || !hasPendingAttachmentProcessing) return
    const sessionId = activeSessionId
    const refreshAttachments = async () => {
      try {
        const items = await apiClient.listAttachments(sessionId)
        if (useChatStore.getState().activeSessionId === sessionId) setAttachments(items)
      } catch {
        // Keep the current status visible and try again on the next poll.
      }
    }
    const timer = window.setInterval(() => void refreshAttachments(), 1000)
    return () => window.clearInterval(timer)
  }, [activeSessionId, hasPendingAttachmentProcessing, setAttachments])

  useEffect(() => {
    if (!showActivity) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setShowActivity(false)
    }
    window.addEventListener("keydown", closeOnEscape)
    return () => window.removeEventListener("keydown", closeOnEscape)
  }, [showActivity])

  useEffect(() => {
    latestInputRef.current = input
  }, [input])

  useEffect(() => {
    const previousKey = previousComposerKeyRef.current
    composerDraftsRef.current.set(previousKey, latestInputRef.current)
    previousComposerKeyRef.current = composerKey
    setInput(composerDraftsRef.current.get(composerKey) ?? "")
    setQueuedFiles(queuedFilesByTargetRef.current.get(composerKey) ?? [])
  }, [composerKey])

  useEffect(() => {
    if (activeSessionId || !draft) {
      setSupportedAttachmentTypes(null)
      return
    }
    let cancelled = false
    void apiClient.getSupportedAttachmentTypes()
      .then((types) => { if (!cancelled) setSupportedAttachmentTypes(types) })
      .catch(() => { if (!cancelled) setSupportedAttachmentTypes(null) })
    return () => { cancelled = true }
  }, [activeSessionId, draft?.id])

  // 加载会话历史消息 — 切换 session 时完全重置状态
  useEffect(() => {
    let cancelled = false

    if (demoMode) {
      setIsLoadingHistory(false)
      return () => { cancelled = true }
    }

    if (activeSessionId) {
      shouldFollowLatestRef.current = true
      // 先完全清除上一个 session 的所有残留状态
      clearMessages()
      clearSteps()
      clearToolCalls()
      clearOrchestrationTasks()
      clearOrchestrationPlans()
      clearApprovals()
      clearThinking()
      setAgentStatus("idle")
      clearError()
      // 再加载新 session 的历史
      loadingSessionIdRef.current = activeSessionId
      setIsLoadingHistory(true)
      loadHistory(activeSessionId, () => cancelled)
      setSelectedAttachmentIds([])
      setSupportedAttachmentTypes(null)
      apiClient.listAttachments(activeSessionId)
        .then((items) => { if (!cancelled) setAttachments(items) })
        .catch(() => { if (!cancelled) setAttachments([]) })
      apiClient.getSupportedAttachmentTypes(activeSessionId)
        .then((types) => { if (!cancelled) setSupportedAttachmentTypes(types) })
        .catch(() => { if (!cancelled) setSupportedAttachmentTypes(null) })
    } else {
      shouldFollowLatestRef.current = true
      setIsLoadingHistory(false)
      clearMessages()
      clearSteps()
      clearToolCalls()
      clearOrchestrationTasks()
      clearOrchestrationPlans()
      clearApprovals()
      clearThinking()
      setAgentStatus("idle")
      clearError()
      setAttachments([])
      setSelectedAttachmentIds([])
      setSupportedAttachmentTypes(null)
    }

    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId, demoMode])

  const loadHistory = async (sessionId: string, isCancelled?: () => boolean) => {
    try {
      // 运行过程由 SSE Durable Event replay 重建；这里只读取持久消息正文。
      const history = await apiClient.getMessages(sessionId)
      // StrictMode 双调用或快速切换 session 时取消应用结果
      if (isCancelled?.()) return
      // 防止快速切换 session 导致的竞态：只在当前 session 仍匹配时应用结果
      if (loadingSessionIdRef.current === sessionId) {
        // 历史请求与 SSE replay 可能并发完成。以消息 ID 和 run_id 合并，
        // 保留已经由 SSE 构造出的实时 assistant 内容，禁止历史响应无条件清空。
        const current = useChatStore.getState().messages
        const mergedById = new Map(history.map((message) => [message.id, message]))
        for (const live of current) {
          const persisted = mergedById.get(live.id)
          if (persisted) {
            mergedById.set(live.id, persisted)
            continue
          }
          const sameRun = live.role === "assistant" && live.run_id
            ? [...mergedById.values()].filter((item) => item.role === "assistant" && item.run_id === live.run_id)
            : []
          if (sameRun.length === 1) {
            const stored = sameRun[0]
            mergedById.set(stored.id, stored)
          } else {
            mergedById.set(live.id, live)
          }
        }
        const merged = [...mergedById.values()].sort((a, b) => {
          const delta = new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
          return Number.isNaN(delta) || delta === 0 ? a.id.localeCompare(b.id) : delta
        })
        setMessages(merged)
      }
    } catch {
      if (isCancelled?.()) return
      if (loadingSessionIdRef.current === sessionId) {
        setError("Failed to load session history. Please try again.")
      }
    } finally {
      if (!isCancelled?.() && loadingSessionIdRef.current === sessionId) {
        setIsLoadingHistory(false)
      }
    }
  }

  const handleMessagesScroll = useCallback(() => {
    const container = messagesContainerRef.current
    if (!container) return
    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight
    // A small threshold prevents sub-pixel/layout changes from disabling
    // follow mode when the user is effectively at the bottom.
    shouldFollowLatestRef.current = distanceFromBottom <= 64
  }, [])

  // 自动滚动到底部，但仅在用户没有主动查看历史消息时跟随。
  useEffect(() => {
    if (!shouldFollowLatestRef.current) return
    const container = messagesContainerRef.current
    if (!container) return
    // Use an immediate scroll here. Smooth scrolling emits intermediate
    // scroll events while content is streaming and can incorrectly look like
    // a user scroll-away.
    container.scrollTo({ top: container.scrollHeight, behavior: "auto" })
  }, [messages, thinking, pendingApprovals, executionTimeline])

  // 自动调整 textarea 高度
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = "auto"
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`
  }, [input])

  const handleSend = useCallback(async () => {
    const text = input.trim()
    const status = useChatStore.getState().agentStatus
    const currentlyActive = Boolean(activeRunId || hasQueuedRuns)
      || ["queued", "thinking", "running", "cancelling", "waiting_approval"].includes(status)
    if ((!text && selectedAttachmentIds.length === 0) || (!activeSessionId && !draft) || isSending || isUploading || hasPendingSelectedAttachments || (currentlyActive && !stopAllowsSend)) return

    const targetComposerId = activeSessionId ?? draft!.id
    runStateRefreshGenerationRef.current += 1
    setIsSending(true)
    const fingerprint = JSON.stringify([text, selectedAttachmentIds])
    if (pendingSendsRef.current.get(targetComposerId)?.fingerprint !== fingerprint) {
      const prior = pendingSendsRef.current.get(targetComposerId)
      if (prior) useChatStore.getState().removeMessage(prior.requestId)
      pendingSendsRef.current.set(targetComposerId, {
        fingerprint,
        requestId: crypto.randomUUID(),
      })
    }
    const pending = pendingSendsRef.current.get(targetComposerId)!
    const optimisticId = pending.requestId
    if (!useChatStore.getState().messages.some((message) => message.id === optimisticId)) {
      addMessage({
        id: optimisticId,
        role: "user",
        content: text || "Please process the attached files.",
        timestamp: new Date().toISOString(),
        session_id: activeSessionId ?? "",
      })
    }
    try {
      composerDraftsRef.current.set(targetComposerId, input)
      const result = await sendEvent(ClientEventType.USER_COMMAND, activeSessionId, {
        ...(draft ? { draft_id: draft.id, project_id: draft.projectId } : {}),
        message: text || "Please process the attached files.",
        request_id: pending.requestId,
        attachment_ids: selectedAttachmentIds,
      })
      if (!result) throw new Error("Run submission returned no receipt")
      runStateRefreshGenerationRef.current += 1
      const currentMessages = useChatStore.getState().messages
      useChatStore.getState().removeMessage(optimisticId)
      if (!currentMessages.some((message) => message.id === result.messageId)) {
        addMessage({
          id: result.messageId,
          role: "user",
          content: text || "Please process the attached files.",
          timestamp: new Date().toISOString(),
          session_id: result.sessionId,
          run_id: result.runId,
        })
      }
      setRunSequenceState((current) => ({ ...current, [result.runId]: result.sequence }))
      composerDraftsRef.current.set(targetComposerId, "")
      composerDraftsRef.current.set(result.sessionId, "")
      pendingSendsRef.current.delete(targetComposerId)
      const currentComposerId = composerKeyRef.current
      if (currentComposerId === targetComposerId || currentComposerId === result.sessionId) {
        setInput("")
        setSelectedAttachmentIds([])
        setAgentStatus(result.status === "queued" ? "queued" : "running")
        setHasQueuedRuns(true)
        setStopAllowsSend(false)
        clearThinking()
        clearError()
      }
    } catch (error) {
      if (composerKeyRef.current === targetComposerId) {
        setError(error instanceof Error ? error.message : "消息发送失败，请重试。")
      }
    }
    setIsSending(false)
  }, [
    input,
    activeSessionId,
    draft,
    isSending,
    stopAllowsSend,
    activeRunId,
    hasQueuedRuns,
    addMessage,
    setAgentStatus,
    setError,
    clearError,
    clearThinking,
    sendEvent,
    selectedAttachmentIds,
    hasPendingSelectedAttachments,
    isUploading,
  ])

  const uploadFiles = useCallback(async (files: File[]) => {
    if ((!activeSessionId && !draft) || files.length === 0 || isUploading || isSending) return
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
    const initialTarget = activeSessionId ?? draft!.id
    queuedFilesByTargetRef.current.set(initialTarget, files)
    setQueuedFiles(files)
    setIsUploading(true)
    clearError()
    let sessionId = activeSessionId
    try {
      sessionId = sessionId ?? await ensureSession(
        draft!.id,
        draft!.projectId,
        _attachmentTitle(files),
      )
      if (initialTarget !== sessionId) {
        queuedFilesByTargetRef.current.delete(initialTarget)
        queuedFilesByTargetRef.current.set(sessionId, files)
      }
      composerDraftsRef.current.set(sessionId, input)
      const created = await apiClient.uploadSessionAttachments(sessionId, files)
      queuedFilesByTargetRef.current.delete(sessionId)
      if (useChatStore.getState().activeSessionId !== sessionId) return
      created.forEach(upsertAttachment)
      setQueuedFiles([])
      setSelectedAttachmentIds((current) => [
        ...current,
        ...created.map((item) => item.id).filter((id) => !current.includes(id)),
      ])
    } catch (err) {
      const target = sessionId ?? initialTarget
      queuedFilesByTargetRef.current.set(target, files)
      if (composerKeyRef.current === target) {
        setQueuedFiles(files)
        setError(err instanceof Error ? err.message : "附件上传失败；文件仍保留，可重试。")
      }
    } finally {
      setIsUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ""
    }
  }, [activeSessionId, draft, ensureSession, input, clearError, isUploading, isSending, setError, supportedAttachmentTypes, upsertAttachment])

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
      if ((!activeSessionId && !draft) || isUploading || isSending || !event.clipboardData) return
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
  }, [activeSessionId, draft, handlePasteUpload, isUploading, isSending])

  const handleDeleteAttachment = useCallback(async (fileId: string) => {
    if (!activeSessionId) return
    await apiClient.deleteAttachment(activeSessionId, fileId)
    setSelectedAttachmentIds((current) => current.filter((id) => id !== fileId))
    removeAttachment(fileId)
  }, [activeSessionId, removeAttachment])

  const refreshRunState = useCallback(async (sessionId: string) => {
    const generation = runStateRefreshGenerationRef.current
    const requestSequence = ++runStateRequestSequenceRef.current
    const runs = await apiClient.listSessionRuns(sessionId)
    if (generation !== runStateRefreshGenerationRef.current || requestSequence < runStateAppliedSequenceRef.current || useChatStore.getState().activeSessionId !== sessionId) return
    runStateAppliedSequenceRef.current = requestSequence
    setRunSequenceState(Object.fromEntries(runs.map((run) => [run.run_id, run.sequence])))
    const active = runs.find((run) => run.status === "running" || run.status === "stopping")
    const queued = runs.some((run) => run.status === "queued")
    const pendingStopKey = active && stopRequestsRef.current.has(`${sessionId}:${active.run_id}`)
      ? `${sessionId}:${active.run_id}`
      : null
    let removedStopRequest = false
    for (const key of stopRequestsRef.current.keys()) {
      if (key.startsWith(`${sessionId}:`) && key !== pendingStopKey) {
        stopRequestsRef.current.delete(key)
        removedStopRequest = true
      }
    }
    if (removedStopRequest) persistPendingStopRequests(stopRequestsRef.current)
    updateSession(sessionId, { active_run_id: active?.run_id ?? null })
    setHasQueuedRuns(queued)
    setStopAllowsSend(!queued && (active?.status === "stopping" || pendingStopKey !== null))
    setStopRequestUncertain(Boolean(pendingStopKey))
    setAgentStatus(active?.status === "stopping" ? "cancelling" : active ? "running" : queued ? "queued" : "idle")
  }, [setAgentStatus, setRunSequenceState, setStopAllowsSend, setStopRequestUncertain, updateSession])

  useEffect(() => {
    if (!activeSessionId || demoMode) return
    void refreshRunState(activeSessionId).catch(() => undefined)
    const timer = window.setInterval(() => void refreshRunState(activeSessionId).catch(() => undefined), 1000)
    return () => window.clearInterval(timer)
  }, [activeSessionId, demoMode, refreshRunState])

  useEffect(() => {
    runStateRefreshGenerationRef.current += 1
    setStopAllowsSend(false)
    setHasQueuedRuns(false)
    setStopRequestUncertain(false)
    setRunSequenceState({})
  }, [activeSessionId])

  const handleStop = useCallback(() => {
    if (!activeSessionId || !activeRunId) return
    const runId = activeRunId
    const requestKey = `${activeSessionId}:${runId}`
    const cursor = Math.max(
      0,
      ...runTimelineEvents.map((event) => event.session_seq),
      ...messages.map((message) => message.session_seq ?? 0),
    )
    const existing = stopRequestsRef.current.get(requestKey)
    const stopRequest = existing ?? { requestId: crypto.randomUUID(), cursor }
    runStateRefreshGenerationRef.current += 1
    stopRequestsRef.current.set(requestKey, stopRequest)
    persistPendingStopRequests(stopRequestsRef.current)
    setStopRequestUncertain(false)
    clearError()
    suppressRunOutput(runId, stopRequest.cursor)
    setStopAllowsSend(!hasQueuedRuns)
    setAgentStatus("cancelling")
    void apiClient.stopSession(activeSessionId, stopRequest.requestId, {
      expected_run_id: runId,
      displayed_session_seq: stopRequest.cursor,
    }).then(() => {
      stopRequestsRef.current.delete(requestKey)
      persistPendingStopRequests(stopRequestsRef.current)
      if (useChatStore.getState().activeSessionId !== activeSessionId) return
      setStopRequestUncertain(false)
      void refreshRunState(activeSessionId).catch(() => undefined)
    }).catch((err) => {
      if (useChatStore.getState().activeSessionId !== activeSessionId) return
      const status = err && typeof err === "object" && "status" in err
        ? Number((err as { status?: unknown }).status)
        : 0
      if (status >= 400 && status < 500) {
        stopRequestsRef.current.delete(requestKey)
        persistPendingStopRequests(stopRequestsRef.current)
        setStopRequestUncertain(false)
        restoreRunOutput(runId)
        setStopAllowsSend(false)
        void refreshRunState(activeSessionId).catch(() => undefined)
      } else setStopRequestUncertain(true)
      setError(err instanceof Error ? err.message : "Failed to stop run")
    })
  }, [activeSessionId, activeRunId, hasQueuedRuns, runTimelineEvents, messages, clearError, suppressRunOutput, setAgentStatus, setError, refreshRunState, restoreRunOutput])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleApproval = useCallback(async (
    batchId: string,
    requests: ApprovalRequest[],
    decisions: Record<string, "approved" | "denied" | "cancelled">,
  ) => {
    if (demoMode) {
      const store = useChatStore.getState()
      Object.entries(decisions).forEach(([id, decision]) => store.resolveApproval(id, decision))
      store.setThinking({ active: false, content: "", messageId: null })
      const approved = Object.values(decisions).every((decision) => decision === "approved")
      const workerA = "demo-worker-feedback"
      const workerB = "demo-worker-review"
      const planId = "demo-plan"
      if (approved) {
        store.updateToolCall("demo-tool-call", { status: "success", output: "已创建高优先级项目事项" })
        store.updateStep("demo-tool-step", { status: "completed", completed_at: new Date().toISOString(), duration_ms: 430 })
        store.upsertOrchestrationTask({ plan_id: planId, task_id: workerA, title: "整理客户反馈", status: "completed", run_id: "demo-run", worker_run_id: workerA, attempt: 1, result_summary: "已提炼响应速度、数据导出和权限配置三个主要问题。", completed_at: new Date().toISOString() })
        store.upsertOrchestrationTask({ plan_id: planId, task_id: workerB, title: "独立复核问题", status: "completed", run_id: "demo-run", worker_run_id: workerB, attempt: 2, result_summary: "复核结果与客户反馈整理一致。", completed_at: new Date().toISOString() })
        store.upsertOrchestrationPlan({ plan_id: planId, run_id: "demo-run", status: "completed", synthesis_status: "completed" })
        store.upsertExecutionTimelineEntry({ id: "demo-synthesis", run_id: "demo-run", event_type: "synthesis.completed", label: "处理结果已汇总", detail: "两个并行任务的结果已合并。", status: "completed", timestamp: new Date().toISOString() })
        store.upsertExecutionTimelineEntry({ id: "demo-approval", run_id: "demo-run", event_type: "approval.resolved", label: "确认已提交", detail: "你已允许此操作继续。", status: "completed", timestamp: new Date().toISOString() })
        store.addMessage({ id: "demo-assistant-message", role: "assistant", content: "已完成本周客户反馈整理：主要问题是响应速度、数据导出和权限配置。我已将高优先级汇总事项写入项目看板（ATH-204）。", timestamp: new Date().toISOString(), session_id: "demo-session", run_id: "demo-run" })
      } else {
        store.updateToolCall("demo-tool-call", { status: "denied", error: "操作已由用户拒绝" })
        store.updateStep("demo-tool-step", { status: "completed", completed_at: new Date().toISOString(), duration_ms: 0 })
        store.upsertOrchestrationTask({ plan_id: planId, task_id: workerA, title: "整理客户反馈", status: "cancelled", run_id: "demo-run", worker_run_id: workerA, attempt: 1, error: "你拒绝了创建项目事项的操作。", completed_at: new Date().toISOString() })
        store.upsertOrchestrationTask({ plan_id: planId, task_id: workerB, title: "独立复核问题", status: "completed", run_id: "demo-run", worker_run_id: workerB, attempt: 2, result_summary: "复核结果已保留。", completed_at: new Date().toISOString() })
        store.upsertOrchestrationPlan({ plan_id: planId, run_id: "demo-run", status: "completed", synthesis_status: "completed" })
        store.upsertExecutionTimelineEntry({ id: "demo-synthesis", run_id: "demo-run", event_type: "synthesis.completed", label: "处理结果已汇总", detail: "已保留复核结果，未执行需要确认的写入操作。", status: "completed", timestamp: new Date().toISOString() })
        store.upsertExecutionTimelineEntry({ id: "demo-approval", run_id: "demo-run", event_type: "approval.resolved", label: "确认已提交", detail: "你已拒绝此操作。", status: "completed", timestamp: new Date().toISOString() })
        store.addMessage({ id: "demo-assistant-message", role: "assistant", content: "反馈整理已完成。我没有创建项目看板事项；你可以继续查看整理结果或调整后再提交。", timestamp: new Date().toISOString(), session_id: "demo-session", run_id: "demo-run" })
      }
      store.setAgentStatus("idle")
      store.upsertExecutionTimelineEntry({ id: `demo-run:${batchId}`, run_id: "demo-run", event_type: "run.completed", label: approved ? "任务已完成" : "操作已拒绝", detail: approved ? "已在项目看板创建事项 ATH-204。" : "项目看板未发生变更。", status: "completed", timestamp: new Date().toISOString() })
      return
    }
    try {
      await apiClient.respondApprovalsIndividually(requests, decisions)
      clearError()
    } catch (err) {
      const message = err instanceof Error ? err.message : "Approval response failed"
      setError(message)
      throw err
    }
  }, [clearError, demoMode, setError])

  useEffect(() => {
    if (!isAgentActive) return
    const timer = window.setInterval(() => setActivityNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [isAgentActive])

  const runSequenceById = new Map<string, number>(Object.entries(runSequenceState))
  for (const event of runTimelineEvents) {
    if (event.event_type === "run.queued" && event.run_id && typeof event.payload.sequence === "number") {
      runSequenceById.set(event.run_id, event.payload.sequence)
    }
  }
  const orderedMessages = [...messages].sort((left, right) => {
    const leftSequence = left.run_id ? runSequenceById.get(left.run_id) : undefined
    const rightSequence = right.run_id ? runSequenceById.get(right.run_id) : undefined
    if (leftSequence !== undefined && rightSequence !== undefined && leftSequence !== rightSequence) {
      return leftSequence - rightSequence
    }
    const delta = Date.parse(left.timestamp) - Date.parse(right.timestamp)
    return Number.isNaN(delta) || delta === 0 ? left.id.localeCompare(right.id) : delta
  })

  const runStartById = new Map<string, { timestamp: string; sessionSeq?: number }>()
  const runTerminalById = new Map<string, { eventType: string; timestamp: string }>()
  for (const event of runTimelineEvents) {
    if (!event.run_id) continue
    if (event.event_type === "run.started" || event.event_type === "run.resumed") {
      if (!runStartById.has(event.run_id) || event.event_type === "run.started") {
        runStartById.set(event.run_id, { timestamp: typeof event.payload.started_at === "string" ? event.payload.started_at : event.occurred_at, sessionSeq: event.session_seq })
      }
      if (event.event_type === "run.resumed") runTerminalById.delete(event.run_id)
    }
    if (["run.completed", "run.failed", "run.cancelled", "run.budget_exceeded"].includes(event.event_type)) {
      runTerminalById.set(event.run_id, { eventType: event.event_type, timestamp: event.occurred_at })
    }
  }
  const activeExecutionEntry = activeRunId
    ? [...executionTimeline].reverse().find((entry) => entry.status === "running" && entry.run_id === activeRunId)
    : undefined
  const runIds = new Set([
    ...runStartById.keys(),
    ...orderedMessages.map((message) => message.run_id).filter((runId): runId is string => Boolean(runId)),
    ...executionTimeline.map((entry) => entry.run_id).filter((runId): runId is string => Boolean(runId)),
  ])
  const runHeaders: ChatFeedItem[] = [...runIds].flatMap((runId) => {
    const started = runStartById.get(runId)
    if (!started) return []
    return [{ kind: "run_header", id: `run-header:${runId}`, timestamp: started.timestamp, runId, sessionSeq: started.sessionSeq, runSequence: runSequenceById.get(runId) }]
  })

  const timelineFeed: ChatFeedItem[] = [
    ...orderedMessages.map((message) => ({
      kind: "message" as const,
      id: `message:${message.id}`,
      timestamp: message.timestamp,
      message,
      sessionSeq: message.session_seq,
      runSequence: message.run_id ? runSequenceById.get(message.run_id) : undefined,
    })),
    ...executionTimeline.map((entry) => ({
      kind: "event" as const,
      id: `event:${entry.id}`,
      timestamp: entry.timestamp,
      entry,
      sessionSeq: entry.session_seq,
      runSequence: entry.run_id ? runSequenceById.get(entry.run_id) : undefined,
    })),
    ...runHeaders,
  ].sort((left, right) => {
    if (left.runSequence !== undefined && right.runSequence !== undefined && left.runSequence !== right.runSequence) {
      return left.runSequence - right.runSequence
    }
    const delta = Date.parse(left.timestamp) - Date.parse(right.timestamp)
    if (!Number.isNaN(delta) && delta !== 0) return delta
    if (left.sessionSeq !== undefined && right.sessionSeq !== undefined && left.sessionSeq !== right.sessionSeq) {
      return left.sessionSeq - right.sessionSeq
    }
    return left.id.localeCompare(right.id)
  })

  return (
    <div ref={rootRef} className="chat-workspace-chat flex-1 flex flex-row min-w-0 min-h-0">
      <div className="flex-1 flex flex-col min-w-0 min-h-0 relative">
      {/* 消息区域 */}
      <div
        ref={messagesContainerRef}
        onScroll={handleMessagesScroll}
        className="flex-1 overflow-y-auto min-h-0"
      >
        {/* Activity 面板开关，固定在消息区右上角 */}
          <div className="absolute top-3 right-4 z-10">
            <div className="flex items-center gap-2">
            <button type="button" onClick={() => { setShowActivity(false); onOpenSessions?.() }} className="inline-flex items-center justify-center rounded-lg border border-athena-border bg-athena-surface p-1.5 text-athena-muted hover:text-athena-text md:hidden" title="打开会话列表" aria-label="打开会话列表"><PanelLeft className="w-4 h-4" /></button>
            <button
              type="button"
              onClick={() => setShowActivity((v) => !v)}
              aria-expanded={showActivity}
              aria-controls="execution-context-panel"
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium border transition-colors ${
                showActivity
                  ? "bg-athena-accent/10 border-athena-accent/40 text-athena-accent"
                  : "bg-athena-surface border-athena-border text-athena-muted hover:text-athena-text hover:bg-athena-bg"
              }`}
              title="切换执行上下文"
            >
              <PanelRight className="w-3.5 h-3.5" />
              执行上下文
            </button>
            </div>
            </div>
        <div className="max-w-3xl mx-auto px-4 py-6">
              {/* 历史消息加载指示器 */}
          {isLoadingHistory && (
            <div className="text-center py-16 text-athena-muted">
              <Loader2 className="w-8 h-8 mx-auto mb-4 text-athena-accent animate-spin" />
              <p className="text-sm">正在加载对话记录…</p>
            </div>
          )}

          {/* 空会话状态 */}
          {!isLoadingHistory && messages.length === 0 && executionTimeline.length === 0 && !thinking && !isAgentActive && (
            <div className="text-center py-16 text-athena-muted">
              <Sparkles className="w-12 h-12 mx-auto mb-4 text-athena-accent" />
              <p className="text-lg font-medium mb-2">{draft ? "开始一段新的聊天" : "How can Athena help you?"}</p>
              <p className="text-sm">
                {draft ? "消息发送或上传附件后，聊天会保存到所选项目。" : "Start a new conversation to begin interacting with your AI agent."}
              </p>
            </div>
          )}

          {/* 错误状态 */}
          {!isLoadingHistory && messages.length === 0 && executionTimeline.length === 0 && error && !thinking && !isAgentActive && (
            <div className="text-center py-16">
              <p className="text-sm text-athena-danger mb-2">{error}</p>
              <p className="text-xs text-athena-muted">
                Try selecting the session again, or create a new one.
              </p>
            </div>
          )}

          {/* 消息与执行事件进入同一时间排序的聊天流。 */}
          {!isLoadingHistory && (messages.length > 0 || executionTimeline.length > 0) && (
            <>
              {timelineFeed.filter((item) => item.kind !== "event" || !item.entry.run_id || !collapsedRunIds.has(item.entry.run_id)).map((item, index, visibleFeed) => {
                const previous = visibleFeed[index - 1]
                const showDateDivider = !previous
                  || dateKey(item.timestamp) !== dateKey(previous.timestamp)
                return (
                  <div key={item.id}>
                    {showDateDivider && (
                      <div className="flex justify-center py-1">
                        <span className="inline-flex items-center px-3 py-1 rounded-full bg-white border border-athena-border text-xs text-black">
                          {formatDateLabel(item.timestamp)}
                        </span>
                      </div>
                    )}
                    {item.kind === "message"
                      ? <MessageBubble message={item.message} isStreaming={item.message.stream_status === "streaming"} />
                      : item.kind === "event"
                        ? <ExecutionTimeline entries={[item.entry]} compact />
                        : <RunActivityHeader
                            runId={item.runId}
                            startedAt={item.timestamp}
                            endedAt={runTerminalById.get(item.runId)?.timestamp}
                            active={isAgentActive && activeRunId === item.runId}
                            activeLabel={executionTimeline.find((entry) => entry.run_id === item.runId && entry.status === "running")?.label}
                            failed={runTerminalById.get(item.runId)?.eventType === "run.failed" || runTerminalById.get(item.runId)?.eventType === "run.budget_exceeded"}
                            collapsed={collapsedRunIds.has(item.runId)}
                            onToggle={() => setCollapsedRunIds((current) => {
                              const next = new Set(current)
                              if (next.has(item.runId)) next.delete(item.runId)
                              else next.add(item.runId)
                              return next
                            })}
                            now={activityNow}
                          />}
                  </div>
                )
              })}
              {isAgentActive && !activeExecutionEntry && (
                <div className="processing-group py-3">
                  <div className="timeline-node thinking-node">
                    <div className="flex items-center gap-3 text-sm text-athena-muted">
                      {agentStatus === "cancelling"
                        ? <Clock className="h-4 w-4 text-athena-muted" />
                        : <Loader2 className="h-4 w-4 animate-spin text-athena-accent" />}
                      {agentStatus === "cancelling" ? "正在停止任务" : agentStatus === "waiting_approval" ? "等待你的确认" : "正在准备下一步操作"}
                    </div>
                  </div>
                </div>
              )}

            </>
          )}

          {pendingApprovals.length > 0 && (
            <div className="my-4 space-y-3">
              {Object.entries(pendingApprovals.reduce<Record<string, typeof pendingApprovals>>((groups, approval) => {
                const batchId = approval.approval_batch_id || approval.approval_id
                groups[batchId] = [...(groups[batchId] || []), approval]
                return groups
              }, {})).map(([batchId, requests]) => (
                <ApprovalCard key={batchId} batchId={batchId} requests={requests} onSubmit={handleApproval} />
              ))}
            </div>
          )}

        </div>
      </div>

      {/* 输入区域 */}
      <div
        className="composer-shell border-t border-athena-border bg-athena-surface p-4"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault()
          void uploadFiles(Array.from(event.dataTransfer.files))
        }}
      >
        <div className="max-w-3xl mx-auto">
          {queuedFiles.length > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-2 rounded-md border border-athena-danger/30 bg-athena-danger/5 px-3 py-2 text-xs">
              <span className="min-w-0 flex-1 truncate text-athena-muted">上传失败，文件仍保留：{queuedFiles.map((file) => file.name).join("、")}</span>
              <button type="button" onClick={() => void uploadFiles(queuedFiles)} disabled={isUploading} className="text-athena-accent hover:underline disabled:opacity-50">重试上传</button>
            </div>
          )}
          {selectedAttachmentIds.length > 0 && (
            <div className="flex flex-wrap gap-2 mb-2">
              {selectedAttachmentIds.map((id) => {
                const item = attachments.find((attachment) => attachment.id === id)
                return (
                  <div key={id} className="flex items-center gap-2 max-w-full rounded-md border border-athena-border bg-athena-bg px-2 py-1.5 text-xs">
                    {item && (item.status === "uploaded" || item.status === "processing")
                      ? <Loader2 className="w-3.5 h-3.5 animate-spin text-athena-accent flex-shrink-0" />
                      : <Paperclip className="w-3.5 h-3.5 text-athena-accent flex-shrink-0" />}
                    <span className="truncate max-w-[220px]">{item?.filename ?? "Attachment"}</span>
                    <span className={item?.status === "failed" ? "text-athena-danger" : "text-athena-muted"}>
                      {item?.status ?? "unavailable"}
                    </span>
                    <button
                      type="button"
                      title="Remove from message"
                      onClick={() => setSelectedAttachmentIds((current) => current.filter((attachmentId) => attachmentId !== id))}
                      className="text-athena-muted hover:text-athena-text"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </div>
                )
              })}
            </div>
          )}
          <div className="composer-tools flex items-stretch gap-2">
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
              disabled={(!activeSessionId && !draft) || isUploading || isSending || !supportedAttachmentTypes}
              title={
                  !activeSessionId && !draft
                  ? "请先选择项目或聊天"
                  : supportedAttachmentTypes
                    ? "添加文件"
                    : "正在加载支持的文件类型"
              }
              className="inline-flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-lg border border-athena-border bg-athena-bg text-athena-muted hover:text-athena-text disabled:opacity-50"
              aria-label="添加文件"
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
                  activeSessionId || draft
                    ? "向 Athena 发送消息…（Enter 发送，Shift+Enter 换行）"
                    : "请选择或创建会话开始聊天"
                }
                disabled={(!activeSessionId && !draft) || isSending}
                aria-label="向 Athena 发送消息"
                rows={1}
                className="block w-full bg-athena-bg border border-athena-border rounded-lg px-4 py-3 pr-12 text-athena-text placeholder:text-athena-muted focus:outline-none focus:border-athena-accent transition-colors resize-none leading-5"
                style={{ maxHeight: "200px", minHeight: "48px" }}
              />
            </div>
            {isAgentActive && !stopAllowsSend && activeRunId ? (
              <>
                <button
                  onClick={handleStop}
                  disabled={agentStatus === "cancelling"}
                  className="inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed bg-athena-danger text-white hover:opacity-90 flex-shrink-0 min-h-[48px] w-[48px]"
                  title={agentStatus === "cancelling" ? "正在停止运行" : "取消运行"}
                  aria-label={agentStatus === "cancelling" ? "正在停止运行" : "取消运行"}
                >
                  {agentStatus === "cancelling" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Square className="w-4 h-4" />}
                </button>
              </>
            ) : (
              <button
                onClick={handleSend}
                disabled={!canSend || (isAgentActive && !stopAllowsSend)}
                className="inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed bg-athena-accent text-white hover:bg-athena-accent-hover flex-shrink-0 min-h-[48px] w-[48px]"
                title={isAgentActive && !stopAllowsSend ? "等待当前任务完成" : "发送消息"}
                aria-label="发送消息"
              >
                <Send className="w-4 h-4" />
              </button>
            )}
            {stopRequestUncertain && stopAllowsSend && activeRunId && (
              <button
                type="button"
                onClick={handleStop}
                className="inline-flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-lg border border-athena-danger text-athena-danger hover:bg-athena-danger/10"
                title="重试停止当前任务"
                aria-label="重试停止当前任务"
              >
                <Square className="w-4 h-4" />
              </button>
            )}
          </div>
          <p className="text-xs text-athena-muted mt-2 text-center">
            Athena 可能会产生错误信息，请核对重要内容。
          </p>
          {activeSessionId && (
            <button
              type="button"
              onClick={() => setShowFiles((value) => !value)}
              className="mt-2 inline-flex items-center gap-1.5 text-xs text-athena-muted hover:text-athena-text"
            >
              <Files className="w-3.5 h-3.5" />
              {attachments.length} 个文件
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
                    {item.message_id == null && !selectedAttachmentIds.includes(item.id) && (
                      <button
                        type="button"
                        title="Add to message"
                        onClick={() => setSelectedAttachmentIds((current) => [...current, item.id])}
                        className="text-athena-muted hover:text-athena-accent text-[11px]"
                      >
                        Add
                      </button>
                    )}
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

      {/* Execution context panel: the request index remains available for the existing flow;
          the redesigned panel keeps plan/task/timeline context in one place. */}
      {showActivity && (
        <>
          <button type="button" className="execution-context-backdrop fixed inset-0 z-40 bg-black/30 xl:hidden" onClick={() => setShowActivity(false)} aria-label="关闭执行上下文" />
          <ExecutionContextPanel
            id="execution-context-panel"
            messages={messages}
            agentStatus={agentStatus}
            connectionStatus={connectionStatus}
            executionTimeline={executionTimeline}
            orchestrationPlans={orchestrationPlans}
            orchestrationTasks={orchestrationTasks}
            attachments={attachments}
            onClose={() => setShowActivity(false)}
          />
        </>
      )}

    </div>
  )
}

export default Chat

function RunActivityHeader({
  startedAt,
  endedAt,
  active,
  activeLabel,
  failed,
  collapsed,
  onToggle,
  now,
}: {
  runId: string
  startedAt: string
  endedAt?: string
  active: boolean
  activeLabel?: string
  failed: boolean
  collapsed: boolean
  onToggle: () => void
  now: number
}) {
  const start = Date.parse(startedAt)
  const end = endedAt ? Date.parse(endedAt) : now
  const duration = Number.isFinite(start) && Number.isFinite(end) ? formatRunDuration(Math.max(0, end - start)) : ""
  const label = active
    ? `${activeLabel || "正在处理请求"}${duration ? ` · 已用 ${duration}` : ""}`
    : `${failed ? "任务未能完成" : "用时"}${duration ? ` ${duration}` : ""}`
  return (
    <div className="my-3 border-y border-athena-border/70 py-2">
      <button type="button" onClick={onToggle} aria-expanded={!collapsed} className="flex w-full items-center gap-2 text-left text-sm font-medium text-athena-muted">
        {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        <span className="flex-1">{label}</span>
        {active && <Loader2 className="h-4 w-4 animate-spin text-athena-accent" />}
      </button>
    </div>
  )
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

function formatRunDuration(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes === 0) return `${seconds} 秒`
  return `${minutes} 分钟 ${seconds} 秒`
}
