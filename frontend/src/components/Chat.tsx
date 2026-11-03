import { memo, useState, useEffect, useRef, useCallback } from "react"
import { Send, Square, Loader2, Sparkles, Clock, PanelRight, PanelLeft, Paperclip, Files, X, Trash2, ChevronDown, ChevronRight } from "lucide-react"
import { MessageBubble } from "./MessageBubble"
import { ApprovalCard } from "./ApprovalDialog"
import { ExecutionContextPanel } from "./ExecutionContextPanel"
import { ExecutionTimeline } from "./ExecutionTimeline"
import { useChatStore } from "../store/chatStore"
import { apiClient, ApiError } from "../api/client"
import {
  filterChatTimelineForLoadedRuns,
  historyMessages,
  historyTimeline,
  mergeHistoryMessages,
} from "../utils/sessionHistory"
import { ClientEventType } from "../types/events"
import type { ApprovalRequest, ApprovalStatusSnapshot, ExecutionTimelineEntry, Message, SupportedAttachmentTypes } from "../types"
import { mergePendingApprovals } from "../utils/workerSnapshotProjection"
import { submitAndRefresh } from "../utils/submitAndRefresh"
import { reconcileApprovalSubmission } from "../utils/approvalSubmission"
import { canSubmitNewRun, preserveTerminalRunStatuses, summarizeSessionRuns } from "../utils/runLifecycle"

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
    sessionRunStatuses,
    setSessionRunStatus,
    replaceSessionRunStatuses,
    orchestrationSnapshots,
    beginOrchestrationSnapshotRequest,
    activeSessionId,
    agentStatus,
    connectionStatus,
    pendingApprovals,
    thinking,
    error,
    setMessages,
    setSessionHistoryLoaded,
    setSessionEventCursor,
    addMessage,
    updateSession,
    clearMessages,
    clearSteps,
    clearToolCalls,
    clearOrchestrationTasks,
    clearOrchestrationPlans,
    clearOrchestrationSnapshots,
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
  const [stopRequestUncertain, setStopRequestUncertain] = useState(false)
  const [runSequenceState, setRunSequenceState] = useState<Record<string, number>>({})
  const [isLoadingHistory, setIsLoadingHistory] = useState(false)
  const [hasMoreHistory, setHasMoreHistory] = useState(false)
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
  const historyCursorBySessionRef = useRef(new Map<string, string>())
  const historyPageLoadingRef = useRef(false)
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
  const currentRunStatuses = activeSessionId ? sessionRunStatuses[activeSessionId] ?? {} : {}
  const activeRunIds = [
    ...(activeRunId ? [activeRunId] : []),
    ...Object.entries(currentRunStatuses)
      .filter(([, status]) => ["queued", "running", "stopping"].includes(status))
      .map(([runId]) => runId),
  ]
  // 聊天时间线只展示已加载轮次和活动 Run，避免低水位 SSE 重放旧终态。
  const chatExecutionTimeline = filterChatTimelineForLoadedRuns(
    executionTimeline,
    messages,
    activeRunIds,
  )
  const hasQueuedRuns = Object.values(currentRunStatuses).some((status) => status === "queued")
  const hasRunningRuns = Object.values(currentRunStatuses).some((status) => status === "running")
  const canSubmitFromRunState = canSubmitNewRun(currentRunStatuses)
  const activeRunStatus = activeRunId ? currentRunStatuses[activeRunId] : undefined
  const stoppingRun = activeRunStatus === "stopping" || (Boolean(activeRunId) && agentStatus === "cancelling")
  const canQueueDuringStopping = stoppingRun && !hasQueuedRuns && !hasRunningRuns
  const latestKnownRunId = activeRunId
    ?? [...runTimelineEvents].reverse().find((event) => event.run_id)?.run_id
    ?? [...messages].reverse().find((message) => message.run_id)?.run_id
  const workerPendingApprovals = (latestKnownRunId ? orchestrationSnapshots[latestKnownRunId]?.approvals : [])
    ?.filter((request) => request.status === "pending")
    .flatMap((request) => request.items.filter((item) => item.status === "pending").map((item) => ({
      approval_id: item.approval_id,
      tool_name: item.tool_name,
      arguments: item.arguments as Record<string, unknown>,
      risk_level: item.risk_level as "low" | "medium" | "high" | "critical",
      approval_batch_id: request.approval_batch_id,
      description: item.summary,
      session_id: activeSessionId || undefined,
      run_id: request.run_id,
      plan_id: request.plan_id,
      task_id: request.task_id,
      expires_at: request.expires_at,
    }))) ?? []
  const canonicalApprovalRequests = latestKnownRunId ? orchestrationSnapshots[latestKnownRunId]?.approvals ?? [] : []
  const visibleApprovals = mergePendingApprovals(pendingApprovals, canonicalApprovalRequests, workerPendingApprovals)
  useEffect(() => {
    if (!activeSessionId || !latestKnownRunId || demoMode) return
    let cancelled = false
    let inFlight = false
    const refresh = async () => {
      if (cancelled || inFlight || document.hidden) return
      inFlight = true
      try {
        const generation = beginOrchestrationSnapshotRequest(latestKnownRunId)
        const snapshot = await apiClient.getRunOrchestration(latestKnownRunId)
        if (cancelled || useChatStore.getState().activeSessionId !== activeSessionId) return
        useChatStore.getState().setOrchestrationSnapshot(snapshot, generation)
      } catch {
        // Preserve the last persisted snapshot while the API is temporarily unavailable.
      } finally {
        inFlight = false
      }
    }
    void refresh()
    const active = Boolean(activeRunId || ["queued", "running", "cancelling", "waiting_approval"].includes(agentStatus))
    const timer = active ? window.setInterval(() => { void refresh() }, 2500) : null
    return () => {
      cancelled = true
      if (timer != null) window.clearInterval(timer)
    }
  }, [activeSessionId, latestKnownRunId, activeRunId, agentStatus, demoMode])
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
    && canSubmitFromRunState
    && (!isAgentActive || canQueueDuringStopping)
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
      if (useChatStore.getState().sessionHistoryLoaded[activeSessionId]) {
        loadingSessionIdRef.current = activeSessionId
        setIsLoadingHistory(false)
        setHasMoreHistory(historyCursorBySessionRef.current.has(activeSessionId))
        setSelectedAttachmentIds([])
        return () => { cancelled = true }
      }
      // 已有缓存包含后台 SSE 投影，保留它并由 setActiveSession 提供隔离后的视图。
      if (!useChatStore.getState().sessionViewCache[activeSessionId]) {
        clearMessages()
        clearSteps()
        clearToolCalls()
        clearOrchestrationTasks()
        clearOrchestrationPlans()
        clearOrchestrationSnapshots()
        clearApprovals()
        clearThinking()
        setAgentStatus("idle")
        clearError()
      }
      // 再加载新 session 的历史
      loadingSessionIdRef.current = activeSessionId
      setIsLoadingHistory(true)
      historyCursorBySessionRef.current.delete(activeSessionId)
      setHasMoreHistory(false)
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
      clearOrchestrationSnapshots()
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

  const loadHistory = async (sessionId: string, isCancelled?: () => boolean, cursor?: string) => {
    try {
      // 运行过程由 SSE Durable Event replay 重建；这里只读取持久消息正文。
      const history = await apiClient.getMessages(sessionId, 20, undefined, cursor)
      // StrictMode 双调用或快速切换 session 时取消应用结果
      if (isCancelled?.()) return
      // 防止快速切换 session 导致的竞态：只在当前 session 仍匹配时应用结果
      if (loadingSessionIdRef.current === sessionId) {
        // 历史请求与 SSE replay 可能并发完成。以消息 ID 和 run_id 合并，
        // 保留已经由 SSE 构造出的实时 assistant 内容，禁止历史响应无条件清空。
        const current = useChatStore.getState().messages
        const persistedMessages = historyMessages(history.items, sessionId)
        const persistedTimeline = historyTimeline(history.items)
        for (const turn of history.items) {
          useChatStore.getState().replaceRunExecutionTimeline(
            turn.run_id,
            persistedTimeline.filter((entry) => entry.run_id === turn.run_id),
          )
        }
        setSessionEventCursor(sessionId, history.snapshot_event_seq)
        setMessages(mergeHistoryMessages(persistedMessages, current))
        setSessionHistoryLoaded(sessionId, true)
        if (history.next_cursor) historyCursorBySessionRef.current.set(sessionId, history.next_cursor)
        else historyCursorBySessionRef.current.delete(sessionId)
        setHasMoreHistory(Boolean(history.next_cursor))
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
    shouldFollowLatestRef.current = distanceFromBottom <= 64
    const cursor = activeSessionId ? historyCursorBySessionRef.current.get(activeSessionId) : undefined
    if (!cursor || !hasMoreHistory || historyPageLoadingRef.current || container.scrollTop > 48 || !activeSessionId) return
    const previousHeight = container.scrollHeight
    historyPageLoadingRef.current = true
    setIsLoadingHistory(true)
    void loadHistory(activeSessionId, undefined, cursor).finally(() => {
      historyPageLoadingRef.current = false
      const currentContainer = messagesContainerRef.current
      if (currentContainer) currentContainer.scrollTop += currentContainer.scrollHeight - previousHeight
    })
  }, [activeSessionId, hasMoreHistory])

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
    const latestStatuses = activeSessionId ? useChatStore.getState().sessionRunStatuses[activeSessionId] ?? {} : {}
    const hasQueuedOrRunningRun = Object.values(latestStatuses).some((runStatus) => runStatus === "queued" || runStatus === "running")
    const activeRunIsStopping = Boolean(activeRunId && latestStatuses[activeRunId] === "stopping")
      || (Boolean(activeRunId) && status === "cancelling")
    const currentlyActive = hasQueuedOrRunningRun
      || (Boolean(activeRunId) && !activeRunIsStopping)
      || (!activeRunId && ["queued", "thinking", "running", "waiting_approval"].includes(status))
    if ((!text && selectedAttachmentIds.length === 0) || (!activeSessionId && !draft) || isSending || isUploading || hasPendingSelectedAttachments || currentlyActive || !canSubmitFromRunState) return

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
      const knownRunStatus = useChatStore.getState().sessionRunStatuses[result.sessionId]?.[result.runId]
      if (!knownRunStatus) {
        setSessionRunStatus(result.sessionId, result.runId, result.status === "queued" ? "queued" : "running")
        if (result.status !== "queued" && !useChatStore.getState().sessions.find((session) => session.id === result.sessionId)?.active_run_id) {
          updateSession(result.sessionId, { active_run_id: result.runId })
        }
      }
      composerDraftsRef.current.set(targetComposerId, "")
      composerDraftsRef.current.set(result.sessionId, "")
      pendingSendsRef.current.delete(targetComposerId)
      const currentComposerId = composerKeyRef.current
      if (currentComposerId === targetComposerId || currentComposerId === result.sessionId) {
        setInput("")
        setSelectedAttachmentIds([])
        if (!knownRunStatus) {
          const summary = summarizeSessionRuns(useChatStore.getState().sessionRunStatuses[result.sessionId] || {})
          setAgentStatus(summary.agentStatus)
        }
        clearThinking()
        if (knownRunStatus !== "failed") clearError()
      }
    } catch (error) {
      useChatStore.getState().removeMessage(optimisticId)
      if (composerKeyRef.current === targetComposerId) {
        const conflictCode = error instanceof ApiError
          ? ["session_run_in_progress", "session_run_queued"].find((code) => error.message.includes(code))
          : undefined
        const message = conflictCode === "session_run_in_progress"
          ? "当前会话有任务正在运行，请等待任务停止后再发送。"
          : conflictCode === "session_run_queued"
            ? "当前会话已有排队任务，请等待其开始后再发送。"
            : error instanceof Error ? error.message : "消息发送失败，请重试。"
        setError(message)
      }
    }
    setIsSending(false)
  }, [
    input,
    activeSessionId,
    draft,
    isSending,
    activeRunId,
    canSubmitFromRunState,
    addMessage,
    setAgentStatus,
    setSessionRunStatus,
    updateSession,
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
    const stateAtStart = useChatStore.getState()
    const statusesAtStart = stateAtStart.sessionRunStatuses[sessionId]
    const revisionAtStart = stateAtStart.sessionRunStatusRevisions[sessionId] || 0
    const runs = await apiClient.listSessionRuns(sessionId)
    if (generation !== runStateRefreshGenerationRef.current || requestSequence < runStateAppliedSequenceRef.current || useChatStore.getState().activeSessionId !== sessionId || useChatStore.getState().sessionRunStatuses[sessionId] !== statusesAtStart || (useChatStore.getState().sessionRunStatusRevisions[sessionId] || 0) !== revisionAtStart) return
    runStateAppliedSequenceRef.current = requestSequence
    setRunSequenceState(Object.fromEntries(runs.map((run) => [run.run_id, run.sequence])))
    const statuses = preserveTerminalRunStatuses(
      useChatStore.getState().sessionRunStatuses[sessionId] || {},
      Object.fromEntries(runs.map((run) => [run.run_id, run.status])),
    )
    if (!replaceSessionRunStatuses(sessionId, statuses, revisionAtStart)) return
    const projected = summarizeSessionRuns(statuses)
    const pendingStopKey = projected.activeRunId && stopRequestsRef.current.has(`${sessionId}:${projected.activeRunId}`)
      ? `${sessionId}:${projected.activeRunId}`
      : null
    let removedStopRequest = false
    for (const key of stopRequestsRef.current.keys()) {
      if (key.startsWith(`${sessionId}:`) && key !== pendingStopKey) {
        stopRequestsRef.current.delete(key)
        removedStopRequest = true
      }
    }
    if (removedStopRequest) persistPendingStopRequests(stopRequestsRef.current)
    updateSession(sessionId, { active_run_id: projected.activeRunId })
    setStopRequestUncertain(Boolean(pendingStopKey))
    setAgentStatus(projected.agentStatus)
  }, [replaceSessionRunStatuses, setAgentStatus, setRunSequenceState, setStopRequestUncertain, updateSession])

  useEffect(() => {
    runStateRefreshGenerationRef.current += 1
    setStopRequestUncertain(false)
    setRunSequenceState({})
  }, [activeSessionId])

  useEffect(() => {
    if (!activeSessionId) return
    const terminalStatuses = new Set(["completed", "failed", "cancelled"])
    let removed = false
    for (const key of stopRequestsRef.current.keys()) {
      if (!key.startsWith(activeSessionId + ":")) continue
      const runId = key.slice(activeSessionId.length + 1)
      if (terminalStatuses.has(currentRunStatuses[runId] || "")) {
        stopRequestsRef.current.delete(key)
        removed = true
      }
    }
    if (removed) {
      persistPendingStopRequests(stopRequestsRef.current)
      setStopRequestUncertain(false)
    }
  }, [activeSessionId, currentRunStatuses])

  useEffect(() => {
    if (!activeSessionId) return
    const active = Object.entries(currentRunStatuses).find(([, status]) => status === "stopping")
    const hasPendingStop = active ? stopRequestsRef.current.has(`${activeSessionId}:${active[0]}`) : false
    if (!active || !hasPendingStop) return
    setStopRequestUncertain(true)
  }, [activeSessionId, currentRunStatuses])

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
        void refreshRunState(activeSessionId).catch(() => undefined)
      } else {
        setStopRequestUncertain(true)
        void refreshRunState(activeSessionId).catch(() => undefined)
      }
      setError(err instanceof Error ? err.message : "Failed to stop run")
    })
  }, [activeSessionId, activeRunId, runTimelineEvents, messages, clearError, suppressRunOutput, setAgentStatus, setError, refreshRunState, restoreRunOutput])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const refreshApprovalStatus = useCallback(async (requests: ApprovalRequest[]): Promise<Map<string, ApprovalStatusSnapshot>> => {
    const statuses = await Promise.all(requests.map((request) => apiClient.getApprovalStatus(request.approval_id)))
    const statusById = new Map(statuses.map((status) => [status.approval_id, status]))
    const runIds = [...new Set(requests.map((request) => request.run_id).filter((runId): runId is string => Boolean(runId)))]
    for (const runId of runIds) {
      const generation = beginOrchestrationSnapshotRequest(runId)
      const snapshot = await apiClient.getRunOrchestration(runId)
      if (useChatStore.getState().activeSessionId === activeSessionId) useChatStore.getState().setOrchestrationSnapshot(snapshot, generation)
    }
    return statusById
  }, [activeSessionId, beginOrchestrationSnapshotRequest])

  const handleApproval = useCallback(async (
    batchId: string,
    requests: ApprovalRequest[],
    decisions: Record<string, "approved" | "denied" | "cancelled">,
    attemptId: string,
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
      return {
        attemptId,
        submittedDecisions: Object.fromEntries(Object.entries(decisions).flatMap(([approvalId, decision]) =>
          decision === "approved" || decision === "denied" ? [[approvalId, decision]] : [],
        )),
        succeededApprovalIds: requests.map((request) => request.approval_id),
        failures: [],
        retryableApprovalIds: [],
        recoverableApprovalIds: [],
        statusUnknownApprovalIds: [],
      }
    }
    let approvalStatuses: Map<string, ApprovalStatusSnapshot> | null = null
    const { submission, refreshed } = await submitAndRefresh(
      () => apiClient.respondApprovalsIndividually(requests, decisions, attemptId),
      async () => {
        approvalStatuses = await refreshApprovalStatus(requests)
      },
    )
    const refreshError = "Decision status could not be confirmed. Reload the run before trying these approvals again."
    if (!submission.failures.length && refreshed) {
      clearError()
    } else if (!refreshed) {
      setError("Decision submitted, but the run status could not be refreshed. Reload the run to check its current state.")
    }
    return reconcileApprovalSubmission(
      submission,
      refreshed ? approvalStatuses : null,
      refreshed ? undefined : refreshError,
    )
  }, [activeSessionId, beginOrchestrationSnapshotRequest, clearError, demoMode, refreshApprovalStatus, setError])

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
    ? [...chatExecutionTimeline].reverse().find((entry) => entry.status === "running" && entry.run_id === activeRunId)
    : undefined
  const runIds = new Set([
    ...runStartById.keys(),
    ...orderedMessages.map((message) => message.run_id).filter((runId): runId is string => Boolean(runId)),
    ...chatExecutionTimeline.map((entry) => entry.run_id).filter((runId): runId is string => Boolean(runId)),
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
    ...chatExecutionTimeline.map((entry) => ({
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
          {isLoadingHistory && messages.length === 0 && chatExecutionTimeline.length === 0 && (
            <div className="text-center py-16 text-athena-muted">
              <Loader2 className="w-8 h-8 mx-auto mb-4 text-athena-accent animate-spin" />
              <p className="text-sm">正在加载对话记录…</p>
            </div>
          )}

          {/* 空会话状态 */}
          {!isLoadingHistory && messages.length === 0 && chatExecutionTimeline.length === 0 && !thinking && !isAgentActive && (
            <div className="text-center py-16 text-athena-muted">
              <Sparkles className="w-12 h-12 mx-auto mb-4 text-athena-accent" />
              <p className="text-lg font-medium mb-2">{draft ? "开始一段新的聊天" : "How can Athena help you?"}</p>
              <p className="text-sm">
                {draft ? "消息发送或上传附件后，聊天会保存到所选项目。" : "Start a new conversation to begin interacting with your AI agent."}
              </p>
            </div>
          )}

          {/* 消息与执行事件进入同一时间排序的聊天流。 */}
          {(messages.length > 0 || chatExecutionTimeline.length > 0) && (
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
                            activeLabel={chatExecutionTimeline.find((entry) => entry.run_id === item.runId && entry.status === "running")?.label}
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
              {isAgentActive && !activeExecutionEntry && ["cancelling", "waiting_approval"].includes(agentStatus) && (
                <div className="processing-group py-3">
                  <div className="timeline-node thinking-node">
                    <div className="flex items-center gap-3 text-sm text-athena-muted">
                      {agentStatus === "cancelling"
                        ? <Clock className="h-4 w-4 text-athena-muted" />
                        : <Loader2 className="h-4 w-4 animate-spin text-athena-accent" />}
                      {agentStatus === "cancelling" ? "正在停止任务" : "等待你的确认"}
                    </div>
                  </div>
                </div>
              )}

            </>
          )}

          {visibleApprovals.length > 0 && (
            <div className="my-4 space-y-3">
              {Object.entries(visibleApprovals.reduce<Record<string, typeof visibleApprovals>>((groups, approval) => {
                const batchId = approval.approval_batch_id || approval.approval_id
                groups[batchId] = [...(groups[batchId] || []), approval]
                return groups
              }, {})).map(([batchId, requests]) => (
                <ApprovalCard key={batchId} batchId={batchId} requests={requests} onSubmit={handleApproval} onRefreshStatus={refreshApprovalStatus} />
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
          {error && <p role="alert" className="mb-2 text-center text-xs text-athena-danger">{error}</p>}
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
            {isAgentActive && activeRunId && !canQueueDuringStopping ? (
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
                disabled={!canSend || (isAgentActive && !canQueueDuringStopping)}
                className="inline-flex items-center justify-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed bg-athena-accent text-white hover:bg-athena-accent-hover flex-shrink-0 min-h-[48px] w-[48px]"
                title={isAgentActive ? "等待当前任务完成" : "发送消息"}
                aria-label="发送消息"
              >
                <Send className="w-4 h-4" />
              </button>
            )}
            {stopRequestUncertain && activeRunId && (
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
            orchestrationSnapshot={latestKnownRunId ? orchestrationSnapshots[latestKnownRunId] : undefined}
            attachments={attachments}
            onClose={() => setShowActivity(false)}
          />
        </>
      )}

    </div>
  )
}

export default memo(Chat)

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
