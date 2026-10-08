import { useState, useEffect, useCallback, useRef } from "react"
import Sidebar from "./components/Sidebar"
import Chat from "./components/Chat"
import StatusIndicator from "./components/StatusIndicator"
import ErrorBoundary from "./components/ErrorBoundary"
import NavRail from "./components/NavRail"
import PageView from "./components/pages/PageView"
import { useChatStore } from "./store/chatStore"
import { useSessionEventStream } from "./hooks/useSessionEventStream"
import { ClientEventType } from "./types/events"
import { apiClient } from "./api/client"
import type { Session, AppView, Message, Step, ToolCall } from "./types"

/** 渲染主应用布局，并协调会话、视图和事件流状态。 */
function AppContent() {
  const {
    sessions,
    activeSessionId,
    setActiveSession,
    addSession,
    setSessions,
    removeSession,
    activeView,
    setActiveView,
  } = useChatStore()

  const [apiBase] = useState("")
  const initializedSessions = useRef(false)
  const [mobileSessionsOpen, setMobileSessionsOpen] = useState(false)
  const demoMode = useState(() => new URLSearchParams(window.location.search).get("demo") === "execution")[0]

  useEffect(() => {
    const initApi = async () => {
      apiClient.setBaseUrl("")
    }
    initApi()
  }, [])

  useEffect(() => {
    if (demoMode) return
    if (initializedSessions.current) return
    initializedSessions.current = true
    const loadSessions = async () => {
      try {
        const list = await apiClient.listSessions()
        // 整体替换而非逐条前插：后端已按 updated_at desc（最近活跃在前）排序，
        // 逐条 addSession 会前插倒转顺序，导致侧栏最旧会话排在最上
        setSessions(list)
        if (list.length > 0) {
          const current = useChatStore.getState().activeSessionId
          setActiveSession(current && list.some((session) => session.id === current) ? current : list[0].id)
        } else {
          const created = await apiClient.createSession("New Session")
          setSessions([{
            id: created.id,
            title: created.title,
            status: created.status as Session["status"],
            created_at: created.created_at,
            updated_at: created.created_at,
          }])
          setActiveSession(created.id)
        }
      } catch {
        useChatStore.getState().setError("Unable to connect to Athena. Check that the backend is running, then reload.")
      }
    }
    loadSessions()
  }, [demoMode, setActiveSession, setSessions])

  useSessionEventStream(demoMode ? null : activeSessionId, apiBase)

  useEffect(() => {
    if (!demoMode) return
    const sessionId = "demo-session"
    const runId = "demo-run"
    const now = Date.now()
    const demoSession: Session = {
      id: sessionId,
      title: "演示 · 客户反馈整理",
      status: "running",
      created_at: new Date(now - 8 * 60_000).toISOString(),
      updated_at: new Date(now).toISOString(),
    }
    const userMessage: Message = {
      id: "demo-user-message",
      role: "user",
      content: "请整理本周客户反馈，提炼出主要问题，并把高优先级事项写入项目看板。",
      timestamp: new Date(now - 5_000).toISOString(),
      session_id: sessionId,
      run_id: runId,
    }
    const {
      setSessions: setDemoSessions,
      setActiveSession: setDemoActiveSession,
      setMessages,
      setConnectionStatus,
      setAgentStatus: setDemoAgentStatus,
      addStep: addDemoStep,
      updateStep: updateDemoStep,
      addToolCall: addDemoToolCall,
      updateToolCall: updateDemoToolCall,
      addApproval: addDemoApproval,
      setThinking: setDemoThinking,
      upsertExecutionTimelineEntry: addDemoTimeline,
      clearError: clearDemoError,
    } = useChatStore.getState()

    setDemoSessions([demoSession])
    setDemoActiveSession(sessionId)
    setMessages([userMessage])
    setConnectionStatus("connected")
    setDemoAgentStatus("thinking")
    clearDemoError()

    const timers: number[] = []
    const later = (delay: number, task: () => void) => {
      timers.push(window.setTimeout(task, delay))
    }
    later(100, () => addDemoTimeline({ id: "demo-run-start", run_id: runId, event_type: "run.started", label: "开始处理你的请求", status: "running", timestamp: new Date().toISOString() }))
    later(700, () => {
      setDemoThinking({ active: true, content: "我正在归纳反馈主题，并检查哪些事项需要写入项目看板。", messageId: "demo-thinking" })
      addDemoTimeline({ id: "demo-plan", run_id: runId, event_type: "task.started", label: "正在归纳客户反馈", detail: "汇总反馈内容并提炼主要问题。", status: "running", timestamp: new Date().toISOString() })
      const step: Step = { id: "demo-llm-step", session_id: sessionId, run_id: runId, step_number: 1, step_type: "llm_call", status: "running", started_at: new Date().toISOString(), duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 }
      addDemoStep(step)
    })
    later(1800, () => {
      setDemoThinking({ active: true, content: "已识别出 3 个主要问题，正在调用项目看板工具写入高优先级事项。", messageId: "demo-thinking" })
      updateDemoStep("demo-llm-step", { status: "completed", completed_at: new Date().toISOString(), duration_ms: 1080, llm_output_tokens: 142 })
      const toolStep: Step = { id: "demo-tool-step", session_id: sessionId, run_id: runId, step_number: 2, step_type: "tool_execution", status: "running", started_at: new Date().toISOString(), duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 }
      addDemoStep(toolStep)
      addDemoToolCall({ id: "demo-tool-call", tool_name: "project.create_issue", arguments: { title: "高优先级客户反馈汇总", priority: "high", labels: ["customer-feedback"] }, status: "running", started_at: new Date().toISOString(), risk_level: "high", step_id: toolStep.id, run_id: runId } as ToolCall)
      addDemoTimeline({ id: "demo-tool-start", run_id: runId, event_type: "tool.started", label: "正在准备创建项目事项", detail: "这项操作会写入项目看板，需要你确认后才会继续。", status: "waiting", metadata: { tool_name: "project.create_issue", arguments: { title: "高优先级客户反馈汇总", priority: "high", labels: ["customer-feedback"] } }, timestamp: new Date().toISOString() })
      setDemoAgentStatus("waiting_approval")
    })
    later(3400, () => {
      setDemoThinking({ active: false, content: "", messageId: null })
      updateDemoStep("demo-tool-step", { status: "paused", completed_at: null })
      updateDemoToolCall("demo-tool-call", { status: "paused" })
      addDemoApproval({ approval_id: "demo-approval", tool_name: "project.create_issue", arguments: { title: "高优先级客户反馈汇总", priority: "high", labels: ["customer-feedback"] }, risk_level: "high", approval_batch_id: "demo-approval-batch", session_id: sessionId })
      addDemoTimeline({ id: "demo-approval", run_id: runId, event_type: "approval.required", label: "需要你确认这项操作", detail: "批准后将在项目看板创建高优先级的“高优先级客户反馈汇总”。", status: "waiting", timestamp: new Date().toISOString() })
    })

    return () => timers.forEach((timer) => window.clearTimeout(timer))
  }, [demoMode])
  const sendEvent = useCallback((type: string, data: Record<string, unknown> = {}): boolean => {
    if (!activeSessionId) return false
    const run = async () => {
      try {
        if (type === ClientEventType.USER_COMMAND) {
          await apiClient.submitRun(activeSessionId, {
            message: String(data.message || ""),
            attachment_ids: Array.isArray(data.attachment_ids)
              ? data.attachment_ids.map(String)
              : [],
          })
        } else if (type === ClientEventType.SESSION_STOP) {
          await apiClient.cancelSession(activeSessionId)
        }
      } catch (error) { useChatStore.getState().setError(error instanceof Error ? error.message : "Command failed") }
    }
    void run()
    return true
  }, [activeSessionId])

  const handleNewSession = useCallback(async () => {
    try {
      const now = new Date();
      const year = now.getFullYear();
      const month = String(now.getMonth() + 1).padStart(2, '0');
      const day = String(now.getDate()).padStart(2, '0');
      const hours = String(now.getHours()).padStart(2, '0');
      const minutes = String(now.getMinutes()).padStart(2, '0');
      const seconds = String(now.getSeconds()).padStart(2, '0');
      const milliseconds = String(now.getMilliseconds()).padStart(3, '0');
      const title = `Session ${year}.${month}.${day} ${hours}:${minutes}:${seconds},${milliseconds}`;
      const session = await apiClient.createSession(title)
      addSession({
        id: session.id,
        title: session.title,
        status: session.status as Session["status"],
        created_at: session.created_at,
        updated_at: session.created_at,
      })
      setActiveSession(session.id)
      return session.id
    } catch (err) {
      useChatStore.getState().setError(err instanceof Error ? err.message : "Failed to create session")
      return null
    }
  }, [addSession, setActiveSession])

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      try {
        await apiClient.deleteSession(sessionId)
        removeSession(sessionId)
        if (activeSessionId === sessionId) {
          setActiveSession(useChatStore.getState().sessions[0]?.id ?? null)
        }
      } catch (err) {
        useChatStore.getState().setError(err instanceof Error ? err.message : "Failed to delete session")
      }
    },
    [activeSessionId, removeSession, setActiveSession],
  )

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      setActiveSession(sessionId)
      // 选中即视为最近活跃：把会话浮到列表顶部，
      // 后端 updated_at 只在消息落库时刷新，选中到下一次落库之间需要即时反映
      useChatStore.setState((state) => {
        if (state.sessions[0]?.id === sessionId) return {}
        const idx = state.sessions.findIndex((s) => s.id === sessionId)
        if (idx === -1) return {}
        const sessions = [...state.sessions]
        const [s] = sessions.splice(idx, 1)
        return { sessions: [s, ...sessions] }
      })
    },
    [setActiveSession],
  )

  const handleSelectView = useCallback((view: AppView) => {
    setActiveView(view)
  }, [])

  const handleRenameSession = useCallback(async (sessionId: string, title: string) => {
    try {
      const updated = await apiClient.updateSessionTitle(sessionId, title)
      useChatStore.getState().updateSession(sessionId, { title: updated.title, updated_at: updated.updated_at })
    } catch (error) {
      useChatStore.getState().setError(error instanceof Error ? error.message : "Session rename failed")
      throw error
    }
  }, [])

  return (
    <div className="flex h-screen w-screen flex-col-reverse overflow-hidden md:flex-row">
      <NavRail activeView={activeView} onSelectView={handleSelectView} />
      {activeView === "chat" ? (
        <>
          <Sidebar
            sessions={sessions}
            activeSessionId={activeSessionId}
            onSelectSession={handleSelectSession}
            onNewSession={handleNewSession}
            onDeleteSession={handleDeleteSession}
            onRenameSession={handleRenameSession}
            mobileOpen={mobileSessionsOpen}
            onCloseMobile={() => setMobileSessionsOpen(false)}
          />
          <main className="flex-1 flex flex-col min-w-0 min-h-0">
            <StatusIndicator />
            <Chat sendEvent={sendEvent} onOpenSessions={() => setMobileSessionsOpen(true)} demoMode={demoMode} />
          </main>
        </>
      ) : (
        <PageView view={activeView} />
      )}
    </div>
  )
}

/** 应用根组件，使用错误边界保护主界面。 */
function App() {
  return (
    <ErrorBoundary>
      <AppContent />
    </ErrorBoundary>
  )
}

export default App
