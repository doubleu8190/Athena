import { useState, useEffect, useCallback, useRef } from "react"
import { LogOut, MessageSquare } from "lucide-react"
import Sidebar from "./components/Sidebar"
import Chat from "./components/Chat"
import StatusIndicator from "./components/StatusIndicator"
import ErrorBoundary from "./components/ErrorBoundary"
import { NAV_ITEMS } from "./components/NavRail"
import PageView from "./components/pages/PageView"
import { useChatStore } from "./store/chatStore"
import { useSessionEventStream } from "./hooks/useSessionEventStream"
import { ClientEventType } from "./types/events"
import { apiClient } from "./api/client"
import LoginPage from "./components/LoginPage"
import type { Session, AppView, Message, Step, ToolCall, OrchestrationTask, OrchestrationPlan } from "./types"

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
  const [authState, setAuthState] = useState<"loading" | "authenticated" | "unauthenticated">(demoMode ? "authenticated" : "loading")

  useEffect(() => {
    apiClient.setBaseUrl("")
    apiClient.setUnauthorizedHandler(() => {
      initializedSessions.current = false
      setAuthState("unauthenticated")
    })
    return () => apiClient.setUnauthorizedHandler(null)
  }, [])

  useEffect(() => {
    if (demoMode) return
    let active = true
    void apiClient.getAuthSession()
      .then((session) => {
        if (active) setAuthState(session.authenticated ? "authenticated" : "unauthenticated")
      })
      .catch(() => {
        if (active) setAuthState("unauthenticated")
      })
    return () => { active = false }
  }, [demoMode])

  useEffect(() => {
    if (!mobileSessionsOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileSessionsOpen(false)
    }
    window.addEventListener("keydown", closeOnEscape)
    return () => window.removeEventListener("keydown", closeOnEscape)
  }, [mobileSessionsOpen])

  useEffect(() => {
    if (demoMode || authState !== "authenticated") return
    if (initializedSessions.current) return
    initializedSessions.current = true
    const loadSessions = async () => {
      try {
        const listedSessions = await apiClient.listSessions()
        const emptyDefaultSessions = await Promise.all(listedSessions.map(async (session) => {
          if (session.title !== "New Session") return false
          try {
            return (await apiClient.getMessages(session.id)).length === 0
          } catch {
            return false
          }
        }))
        const list = listedSessions.filter((_, index) => !emptyDefaultSessions[index])
        // 整体替换而非逐条前插：后端已按 updated_at desc（最近活跃在前）排序，
        // 逐条 addSession 会前插倒转顺序，导致侧栏最旧会话排在最上
        setSessions(list)
        if (list.length > 0) {
          const current = useChatStore.getState().activeSessionId
          setActiveSession(current && list.some((session) => session.id === current) ? current : list[0].id)
        } else {
          setActiveSession(null)
        }
      } catch {
        useChatStore.getState().setError("Unable to connect to Athena. Check that the backend is running, then reload.")
      }
    }
    loadSessions()
  }, [authState, demoMode, setActiveSession, setSessions])

  useSessionEventStream(demoMode || authState !== "authenticated" ? null : activeSessionId, apiBase)

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
      addToolCall: addDemoToolCall,
      addApproval: addDemoApproval,
      upsertOrchestrationPlan: upsertDemoPlan,
      upsertOrchestrationTask: upsertDemoTask,
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
    const planId = "demo-plan"
    const workerA = "demo-worker-feedback"
    const workerB = "demo-worker-review"
    const makeTask = (task_id: string, title: string, status: OrchestrationTask["status"]): OrchestrationTask => ({
      plan_id: planId, task_id, run_id: runId, title, objective: status === "completed" ? "已整理并提炼用户反馈" : "正在处理用户反馈相关信息", status,
      worker_run_id: task_id, attempt: 1, execution_generation: 1,
    })
    const plan: OrchestrationPlan = { plan_id: planId, run_id: runId, goal: "整理客户反馈并复核问题", status: "running", max_parallelism: 2, task_ids: [workerA, workerB], synthesis_status: "idle" }
    upsertDemoPlan(plan)
    upsertDemoTask(makeTask(workerA, "整理客户反馈", "queued"))
    upsertDemoTask(makeTask(workerB, "独立复核问题", "queued"))

    const timers: number[] = []
    const later = (delay: number, task: () => void) => {
      timers.push(window.setTimeout(task, delay))
    }
    later(100, () => addDemoTimeline({ id: "demo-run-start", run_id: runId, event_type: "run.started", label: "开始处理你的请求", status: "running", timestamp: new Date().toISOString() }))
    later(700, () => {
      setDemoThinking({ active: true, content: "我正在拆分任务，让两个工作单元同时整理和复核反馈。", messageId: "demo-thinking" })
      addDemoTimeline({ id: "demo-plan", run_id: runId, event_type: "plan.created", label: "已制定执行计划", detail: "将同时处理客户反馈整理和问题复核。", status: "running", timestamp: new Date().toISOString() })
      upsertDemoTask({ ...makeTask(workerA, "整理客户反馈", "running"), started_at: new Date().toISOString() })
      upsertDemoTask({ ...makeTask(workerB, "独立复核问题", "running"), started_at: new Date().toISOString() })
    })
    later(1800, () => {
      setDemoThinking({ active: true, content: "两个任务正在并行处理，其中问题复核遇到一次暂时性问题。", messageId: "demo-thinking" })
      upsertDemoTask({ ...makeTask(workerB, "独立复核问题", "retry_wait"), attempt: 2, error: "暂时无法读取一份反馈记录，准备重试。" })
      const reviewStep: Step = { id: "demo-review-step", session_id: sessionId, run_id: workerB, parent_run_id: runId, step_number: 1, step_type: "llm_call", status: "running", started_at: new Date().toISOString(), duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 }
      addDemoStep(reviewStep)
    })
    later(2800, () => {
      setDemoThinking({ active: false, content: "", messageId: null })
      upsertDemoTask({ ...makeTask(workerA, "整理客户反馈", "waiting_approval"), approval_batch_id: "demo-approval-batch", worker_run_id: workerA })
      const toolStep: Step = { id: "demo-tool-step", session_id: sessionId, run_id: workerA, parent_run_id: runId, step_number: 1, step_type: "tool_execution", status: "paused", started_at: new Date().toISOString(), duration_ms: 0, llm_input_tokens: 0, llm_output_tokens: 0 }
      addDemoStep(toolStep)
      addDemoToolCall({ id: "demo-tool-call", tool_name: "project.create_issue", arguments: { title: "高优先级客户反馈汇总", priority: "high" }, status: "paused", started_at: new Date().toISOString(), risk_level: "high", step_id: toolStep.id, run_id: workerA } as ToolCall)
      addDemoApproval({ approval_id: "demo-approval", tool_name: "project.create_issue", arguments: { title: "高优先级客户反馈汇总", priority: "high" }, risk_level: "high", approval_batch_id: "demo-approval-batch", session_id: sessionId, plan_id: planId, task_id: workerA, task_title: "整理客户反馈", worker_run_id: workerA })
      addDemoTimeline({ id: "demo-approval", run_id: runId, event_type: "approval.required", label: "需要确认后才能继续", detail: "任务「整理客户反馈」需要确认；允许后会在项目看板创建高优先级事项。", status: "waiting", timestamp: new Date().toISOString() })
      setDemoAgentStatus("waiting_approval")
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
            ...(data.message_id ? { message_id: String(data.message_id) } : {}),
            attachment_ids: Array.isArray(data.attachment_ids)
              ? data.attachment_ids.map(String)
              : [],
          })
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

  const handleLogout = useCallback(async () => {
    try {
      await apiClient.logout()
    } finally {
      initializedSessions.current = false
      setAuthState("unauthenticated")
    }
  }, [])

  if (authState === "loading") {
    return <main className="flex min-h-screen items-center justify-center bg-athena-bg text-athena-muted">加载中...</main>
  }
  if (authState === "unauthenticated" && !demoMode) {
    return <LoginPage onAuthenticated={() => { initializedSessions.current = false; setAuthState("authenticated") }} />
  }

  return (
    <div className="flex h-screen w-screen flex-col-reverse overflow-hidden md:flex-row">
      {mobileSessionsOpen && (
        <button
          type="button"
          className="chat-drawer-backdrop fixed inset-0 z-20 bg-black/40 md:hidden"
          onClick={() => setMobileSessionsOpen(false)}
          aria-label="关闭工作区导航"
        />
      )}
      <Sidebar
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={(sessionId) => {
          handleSelectSession(sessionId)
          setMobileSessionsOpen(false)
          setActiveView("chat")
        }}
        onNewSession={async () => {
          await handleNewSession()
          setMobileSessionsOpen(false)
          setActiveView("chat")
        }}
        onDeleteSession={handleDeleteSession}
        onRenameSession={handleRenameSession}
        activeView={activeView}
        onSelectView={(view) => {
          setMobileSessionsOpen(false)
          handleSelectView(view)
        }}
        mobileOpen={mobileSessionsOpen}
        onCloseMobile={() => setMobileSessionsOpen(false)}
      />
      <main className="chat-workspace-main flex-1 flex flex-col min-w-0 min-h-0">
        <header className="chat-workspace-header border-b border-athena-border">
          <div className="chat-workspace-title-row">
            <button type="button" onClick={() => setMobileSessionsOpen(true)} className="chat-mobile-session-trigger" title="打开工作区导航" aria-label="打开工作区导航">
              <MessageSquare className="h-4 w-4" />
            </button>
            <div className="min-w-0 flex-1">
              <div className="chat-workspace-title truncate">
                {activeView === "chat"
                  ? sessions.find((session) => session.id === activeSessionId)?.title || "新对话"
                  : NAV_ITEMS.find((item) => item.view === activeView)?.label || "Athena 工作区"}
              </div>
              <div className="chat-workspace-subtitle">Athena 工作区</div>
            </div>
          </div>
          <div className="chat-workspace-header-status"><StatusIndicator /></div>
          {!demoMode && <button type="button" className="btn-ghost mr-3" onClick={() => void handleLogout()} aria-label="退出登录" title="退出登录">
            <LogOut size={16} />
          </button>}
        </header>
        {activeView === "chat" ? (
            <Chat sendEvent={sendEvent} onOpenSessions={() => setMobileSessionsOpen(true)} demoMode={demoMode} />
        ) : (
          <PageView view={activeView} />
        )}
      </main>
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
