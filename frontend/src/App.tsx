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
import type { Session, Project, ChatDraft, AppView, Message, Step, ToolCall, OrchestrationTask, OrchestrationPlan } from "./types"

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
  const createPromises = useRef(new Map<string, Promise<Session>>())
  const [projects, setProjects] = useState<Project[]>([])
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<ChatDraft[]>([])
  const [activeDraft, setActiveDraft] = useState<ChatDraft | null>(null)
  const activeDraftRef = useRef(activeDraft)
  activeDraftRef.current = activeDraft
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
        const [listedProjects, listedSessions] = await Promise.all([
          apiClient.listProjects(),
          apiClient.listSessions(),
        ])
        const list = listedSessions
        setProjects(listedProjects)
        setSessions(list)
        const current = useChatStore.getState().activeSessionId
        if (list.length > 0) {
          const selected = list.find((session) => session.id === current) ?? list[0]
          setActiveSession(selected.id)
          setActiveProjectId(selected.project_id ?? listedProjects[0]?.id ?? null)
        } else {
          setActiveSession(null)
          setActiveProjectId((previous) => previous && listedProjects.some((project) => project.id === previous) ? previous : listedProjects[0]?.id ?? null)
        }
      } catch {
        useChatStore.getState().setError("Unable to connect to Athena. Check that the backend is running, then reload.")
      }
    }
    loadSessions()
  }, [authState, demoMode, setActiveSession, setSessions])

  const refreshSessions = useCallback(async () => {
    const [incoming, currentProjects] = await Promise.all([
      apiClient.listSessions(),
      apiClient.listProjects(),
    ])
    setProjects(currentProjects)
    setSessions(incoming)
  }, [setSessions])

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
  const sendEvent = useCallback(async (type: string, sessionId: string | null, data: Record<string, unknown> = {}): Promise<string | null> => {
    if (type !== ClientEventType.USER_COMMAND) return sessionId
    const draftId = typeof data.draft_id === "string" ? data.draft_id : null
    const projectId = typeof data.project_id === "string" ? data.project_id : undefined
    const result = await apiClient.submitRun(sessionId, {
      project_id: projectId,
      message: String(data.message || ""),
      ...(data.message_id ? { message_id: String(data.message_id) } : {}),
      ...(data.command_id ? { command_id: String(data.command_id) } : {}),
      attachment_ids: Array.isArray(data.attachment_ids) ? data.attachment_ids.map(String) : [],
    })
    if (!sessionId) {
      const targetSessionId = result.session_id
      setDrafts((current) => current.filter((draft) => draft.id !== draftId))
      if (draftId && activeDraftRef.current?.id === draftId) {
        setActiveSession(targetSessionId)
        setActiveProjectId(projectId ?? null)
        setActiveDraft(null)
      }
      try {
        addSession(await apiClient.getSession(targetSessionId))
      } catch {
        // 响应已确定会话 ID；详情读取失败时由侧栏刷新补齐显示字段。
      }
    }
    void refreshSessions().catch(() => undefined)
    return result.session_id
  }, [addSession, refreshSessions, setActiveSession, setDrafts])

  const handleCreateProject = useCallback(async () => {
    const name = window.prompt("项目名称")?.trim()
    if (!name) return
    try {
      const project = await apiClient.createProject(name)
      setProjects((current) => [project, ...current.filter((item) => item.id !== project.id)])
      setActiveProjectId(project.id)
      setActiveSession(null)
      setActiveDraft(null)
      setActiveView("chat")
    } catch (error) {
      useChatStore.getState().setError(error instanceof Error ? error.message : "项目创建失败")
    }
  }, [setActiveSession, setActiveView])

  const handleNewSession = useCallback(async (requestedProjectId?: string) => {
    let projectId = requestedProjectId ?? activeProjectId ?? projects[0]?.id ?? null
    if (!projectId) {
      const name = window.prompt("还没有项目。请输入新项目名称：")?.trim()
      if (!name) return
      try {
        const project = await apiClient.createProject(name)
        setProjects((current) => [project, ...current])
        projectId = project.id
      } catch (error) {
        useChatStore.getState().setError(error instanceof Error ? error.message : "项目创建失败")
        return
      }
    }
    setActiveProjectId(projectId)
    setActiveSession(null)
    const draft = { id: crypto.randomUUID(), projectId }
    setDrafts((current) => [draft, ...current])
    setActiveDraft(draft)
    setActiveView("chat")
  }, [activeProjectId, projects, setActiveSession, setActiveView, setDrafts])

  const handleSelectProject = useCallback((projectId: string) => {
    setActiveProjectId(projectId)
    setActiveSession(null)
    const draft = { id: crypto.randomUUID(), projectId }
    setDrafts((current) => [draft, ...current])
    setActiveDraft(draft)
    setActiveView("chat")
  }, [setActiveSession, setActiveView, setDrafts])

  const ensureSession = useCallback((draftId: string, projectId: string, title?: string): Promise<string> => {
    const inFlight = createPromises.current.get(draftId)
    if (inFlight) return inFlight.then((session) => session.id)
    const creation = apiClient.createSession(projectId, draftId, title).then((value) => {
      const session: Session = {
        id: value.id,
        title: value.title,
        status: value.status as Session["status"],
        created_at: value.created_at,
        updated_at: value.updated_at ?? value.created_at,
        project_id: value.project_id,
        last_activity_at: value.last_activity_at,
      }
      addSession(session)
      setDrafts((current) => current.filter((draft) => draft.id !== draftId))
      if (activeDraftRef.current?.id === draftId) {
        setActiveProjectId(projectId)
        setActiveSession(session.id)
        setActiveDraft(null)
      }
      return session
    })
    createPromises.current.set(draftId, creation)
    return creation
      .then((session) => session.id)
      .catch((error: unknown) => {
        createPromises.current.delete(draftId)
        throw error
      })
      .finally(() => createPromises.current.delete(draftId))
  }, [addSession, setActiveSession, setDrafts])

  const handleDeleteSession = useCallback(
    async (sessionId: string) => {
      try {
        await apiClient.deleteSession(sessionId)
        removeSession(sessionId)
        if (activeSessionId === sessionId) {
          const nextSession = useChatStore.getState().sessions[0] ?? null
          setActiveSession(nextSession?.id ?? null)
          setActiveProjectId(nextSession?.project_id ?? null)
          setActiveDraft(null)
        }
      } catch (err) {
        useChatStore.getState().setError(err instanceof Error ? err.message : "Failed to delete session")
      }
    },
    [activeSessionId, removeSession, setActiveSession],
  )

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      const selected = useChatStore.getState().sessions.find((session) => session.id === sessionId)
      setActiveSession(sessionId)
      setActiveProjectId(selected?.project_id ?? null)
      setActiveDraft(null)
    },
    [setActiveSession],
  )

  const handleRenameProject = useCallback(async (projectId: string, name: string) => {
    try {
      const project = await apiClient.renameProject(projectId, name)
      setProjects((current) => current.map((item) => item.id === project.id ? project : item))
    } catch (error) {
      useChatStore.getState().setError(error instanceof Error ? error.message : "项目重命名失败")
      throw error
    }
  }, [])

  const handleDeleteProject = useCallback(async (projectId: string) => {
    const project = projects.find((item) => item.id === projectId)
    if (!project) return
    const projectSessions = sessions.filter((session) => session.project_id === projectId)
    const confirmed = window.confirm(`删除项目“${project.name}”及其 ${projectSessions.length} 个聊天和项目级记忆？此操作会软删除聊天，并移除该项目的记忆与任务回顾。`)
    if (!confirmed) return
    try {
      await apiClient.deleteProject(projectId)
      const deletedSessionIds = new Set(projectSessions.map((session) => session.id))
      setProjects((current) => current.filter((item) => item.id !== projectId))
      setDrafts((current) => current.filter((draft) => draft.projectId !== projectId))
      setSessions(useChatStore.getState().sessions.filter((session) => !deletedSessionIds.has(session.id)))
      if (activeProjectId === projectId) {
        const nextSession = useChatStore.getState().sessions.find((session) => !deletedSessionIds.has(session.id)) ?? null
        setActiveSession(nextSession?.id ?? null)
        setActiveProjectId(nextSession?.project_id ?? null)
        setActiveDraft(null)
      }
    } catch (error) {
      useChatStore.getState().setError(error instanceof Error ? error.message : "项目删除失败")
    }
  }, [activeProjectId, projects, sessions, setActiveSession, setSessions, setDrafts])

  const handleSelectDraft = useCallback((draftId: string) => {
    const draft = drafts.find((item) => item.id === draftId)
    if (!draft) return
    setActiveProjectId(draft.projectId)
    setActiveSession(null)
    setActiveDraft(draft)
    setActiveView("chat")
  }, [drafts, setActiveSession, setActiveView])

  const handleDeleteDraft = useCallback((draftId: string) => {
    setDrafts((current) => current.filter((draft) => draft.id !== draftId))
    if (activeDraft?.id === draftId) setActiveDraft(null)
  }, [activeDraft, setDrafts])

  const handleSelectView = useCallback((view: AppView) => {
    setActiveView(view)
  }, [])

  const handleRenameSession = useCallback(async (sessionId: string, title: string) => {
    try {
      const updated = await apiClient.updateSessionTitle(sessionId, title)
      useChatStore.getState().updateSession(sessionId, {
        title: updated.title,
        updated_at: updated.updated_at,
      })
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
      setDrafts([])
      setActiveDraft(null)
      setProjects([])
      setActiveProjectId(null)
      setSessions([])
      setActiveSession(null)
      setAuthState("unauthenticated")
    }
  }, [setActiveSession, setSessions, setDrafts])

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
        projects={projects}
        sessions={sessions}
        drafts={drafts}
        activeProjectId={activeProjectId}
        activeSessionId={activeSessionId}
        activeDraftId={activeDraft?.id ?? null}
        onSelectProject={(projectId) => {
          handleSelectProject(projectId)
          setMobileSessionsOpen(false)
          setActiveView("chat")
        }}
        onSelectSession={(sessionId) => {
          handleSelectSession(sessionId)
          setMobileSessionsOpen(false)
          setActiveView("chat")
        }}
        onSelectDraft={(draftId) => {
          handleSelectDraft(draftId)
          setMobileSessionsOpen(false)
        }}
        onDeleteDraft={handleDeleteDraft}
        onNewSession={(projectId) => {
          void handleNewSession(projectId)
          setMobileSessionsOpen(false)
          setActiveView("chat")
        }}
        onCreateProject={() => void handleCreateProject()}
        onRenameProject={handleRenameProject}
        onDeleteProject={(projectId) => void handleDeleteProject(projectId)}
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
                  ? sessions.find((session) => session.id === activeSessionId)?.title || (activeDraft ? "新聊天" : "选择项目")
                  : NAV_ITEMS.find((item) => item.view === activeView)?.label || "Athena 工作区"}
              </div>
              <div className="chat-workspace-subtitle">
                {activeView === "chat"
                  ? projects.find((project) => project.id === activeProjectId)?.name || "Athena 工作区"
                  : "Athena 工作区"}
              </div>
            </div>
          </div>
          <div className="chat-workspace-header-status"><StatusIndicator /></div>
          {!demoMode && <button type="button" className="btn-ghost mr-3" onClick={() => void handleLogout()} aria-label="退出登录" title="退出登录">
            <LogOut size={16} />
          </button>}
        </header>
        {activeView === "chat" ? (
            <Chat
              sendEvent={sendEvent}
              draft={activeDraft}
              ensureSession={ensureSession}
              onOpenSessions={() => setMobileSessionsOpen(true)}
              demoMode={demoMode}
            />
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
