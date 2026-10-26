import {
  Check,
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Folder,
  MessageSquare,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
  X,
} from "lucide-react"
import { useEffect, useState } from "react"
import type { AppView, ChatDraft, Project, Session } from "../types"
import { NAV_ITEMS } from "./NavRail"

interface SidebarProps {
  projects: Project[]
  sessions: Session[]
  drafts: ChatDraft[]
  activeProjectId: string | null
  activeSessionId: string | null
  activeDraftId: string | null
  onSelectProject: (projectId: string) => void
  onSelectSession: (sessionId: string) => void
  onSelectDraft: (draftId: string) => void
  onDeleteDraft: (draftId: string) => void
  onNewSession: (projectId?: string) => void
  onCreateProject: () => void
  onRenameProject: (projectId: string, name: string) => Promise<void>
  onDeleteProject: (projectId: string) => void
  onDeleteSession: (sessionId: string) => void
  onRenameSession?: (sessionId: string, title: string) => Promise<void>
  activeView?: AppView
  onSelectView?: (view: AppView) => void
  mobileOpen?: boolean
  onCloseMobile?: () => void
}

function Sidebar({
  projects,
  sessions,
  drafts,
  activeProjectId,
  activeSessionId,
  activeDraftId,
  onSelectProject,
  onSelectSession,
  onSelectDraft,
  onDeleteDraft,
  onNewSession,
  onCreateProject,
  onRenameProject,
  onDeleteProject,
  onDeleteSession,
  onRenameSession,
  activeView = "chat",
  onSelectView,
  mobileOpen = true,
  onCloseMobile,
}: SidebarProps) {
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("athena:expandedProjects") || "[]")
      return new Set(Array.isArray(saved) ? saved.filter((item): item is string => typeof item === "string") : [])
    } catch {
      return new Set()
    }
  })
  const [editingProjectId, setEditingProjectId] = useState<string | null>(null)
  const [projectNameDraft, setProjectNameDraft] = useState("")
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null)
  const [sessionTitleDraft, setSessionTitleDraft] = useState("")

  useEffect(() => {
    if (!activeProjectId) return
    setExpanded((current) => new Set(current).add(activeProjectId))
  }, [activeProjectId])

  const toggleExpanded = (projectId: string) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(projectId)) next.delete(projectId)
      else next.add(projectId)
      try { localStorage.setItem("athena:expandedProjects", JSON.stringify([...next])) } catch { /* ignore unavailable storage */ }
      return next
    })
  }

  const saveProjectName = async () => {
    if (!editingProjectId || !projectNameDraft.trim()) return
    try {
      await onRenameProject(editingProjectId, projectNameDraft.trim())
      setEditingProjectId(null)
    } catch {
      // The application status area displays the request error.
    }
  }

  const saveSessionTitle = async () => {
    if (!editingSessionId || !sessionTitleDraft.trim() || !onRenameSession) return
    try {
      await onRenameSession(editingSessionId, sessionTitleDraft.trim())
      setEditingSessionId(null)
    } catch {
      // The application status area displays the request error.
    }
  }

  const renderSession = (session: Session) => {
    const isActive = activeSessionId === session.id
    const isRunning = session.status === "running"
    const isCompleted = session.status === "completed"
    return (
      <li key={session.id}>
        <div className={`group mx-2 my-0.5 flex items-center gap-1 rounded-lg px-1 transition-colors ${isActive ? "bg-athena-accent/20 text-athena-text border border-athena-accent/30" : "hover:bg-athena-bg text-athena-muted hover:text-athena-text"}`}>
          {editingSessionId === session.id ? (
            <>
              <input autoFocus value={sessionTitleDraft} onChange={(event) => setSessionTitleDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void saveSessionTitle(); if (event.key === "Escape") setEditingSessionId(null) }} className="min-w-0 flex-1 bg-transparent px-2 py-2 text-sm outline-none" aria-label="聊天标题" />
              <button type="button" onClick={() => void saveSessionTitle()} className="rounded p-2 text-athena-success hover:bg-athena-border/60" title="保存标题" aria-label="保存标题"><Check className="h-3.5 w-3.5" /></button>
            </>
          ) : (
            <>
              <button type="button" onClick={() => onSelectSession(session.id)} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left" aria-current={isActive ? "page" : undefined} title={session.title}>
                <MessageSquare className={`h-3.5 w-3.5 flex-shrink-0 ${isActive ? "text-athena-accent" : isCompleted ? "text-athena-success" : ""}`} />
                <span className="truncate text-sm">{session.title || "未命名聊天"}</span>
                {session.title_pending && <span className="h-1.5 w-1.5 flex-shrink-0 animate-pulse rounded-full bg-athena-accent" title="正在生成标题" />}
                {isRunning && <span className="h-2 w-2 flex-shrink-0 animate-pulse rounded-full bg-athena-accent" title="运行中" />}
                {isCompleted && !isActive && <CheckCircle className="h-3.5 w-3.5 flex-shrink-0 text-athena-success" />}
              </button>
              {onRenameSession && <button type="button" onClick={() => { setEditingSessionId(session.id); setSessionTitleDraft(session.title) }} className="rounded p-2 text-athena-muted opacity-0 transition-opacity hover:bg-athena-border/60 hover:text-athena-text group-hover:opacity-100 group-focus-within:opacity-100" title="重命名聊天" aria-label={`重命名 ${session.title}`}><Pencil className="h-3.5 w-3.5" /></button>}
              <button type="button" onClick={() => onDeleteSession(session.id)} className="rounded p-2 text-athena-muted opacity-0 transition-opacity hover:bg-athena-danger/20 hover:text-athena-danger group-hover:opacity-100 group-focus-within:opacity-100" title="删除聊天" aria-label={`删除 ${session.title}`}><Trash2 className="h-3.5 w-3.5" /></button>
            </>
          )}
        </div>
      </li>
    )
  }

  const renderDraft = (draft: ChatDraft) => (
    <li key={draft.id}>
      <div className={`group mx-2 my-0.5 flex items-center gap-1 rounded-lg px-1 ${activeDraftId === draft.id ? "bg-athena-accent/20 text-athena-text" : "text-athena-muted hover:bg-athena-bg hover:text-athena-text"}`}>
        <button type="button" onClick={() => onSelectDraft(draft.id)} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left" aria-current={activeDraftId === draft.id ? "page" : undefined}>
          <MessageSquare className="h-3.5 w-3.5 flex-shrink-0 text-athena-accent" />
          <span className="truncate text-sm">新聊天草稿</span>
        </button>
        <button type="button" onClick={() => onDeleteDraft(draft.id)} className="rounded p-2 text-athena-muted opacity-0 transition-opacity hover:bg-athena-danger/20 hover:text-athena-danger group-hover:opacity-100 group-focus-within:opacity-100" title="丢弃草稿" aria-label="丢弃新聊天草稿"><Trash2 className="h-3.5 w-3.5" /></button>
      </div>
    </li>
  )

  const completedCount = sessions.filter((session) => session.status === "completed").length
  const activeCount = sessions.filter((session) => session.status === "running").length

  return (
    <aside className={`${mobileOpen ? "flex" : "hidden md:flex"} chat-workspace-sidebar fixed inset-y-0 left-0 z-30 w-[min(88vw,18rem)] md:static md:inset-auto md:w-[248px] flex-shrink-0 bg-athena-surface border-r border-athena-border flex-col h-full shadow-2xl md:shadow-none`}>
      <div className="p-4 border-b border-athena-border">
        <div className="flex items-center gap-2 mb-4">
          <Sparkles className="w-6 h-6 text-athena-accent" />
          <span className="font-semibold text-lg flex-1">Athena</span>
          <button type="button" onClick={onCloseMobile} className="md:hidden btn-ghost p-1.5" title="关闭导航" aria-label="关闭导航"><X className="w-4 h-4" /></button>
        </div>
        <button type="button" onClick={() => onNewSession()} className="w-full btn-primary">
          <Plus className="w-4 h-4" />
          新聊天
        </button>
      </div>

      <nav className="border-b border-athena-border px-2 pb-3" aria-label="工作区导航">
        <div className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-athena-muted">工作区</div>
        <div className="grid grid-cols-2 gap-1">
          {NAV_ITEMS.map(({ view, label, icon: Icon }) => (
            <button key={view} type="button" onClick={() => onSelectView?.(view)} aria-current={activeView === view ? "page" : undefined} className={`flex min-w-0 items-center gap-2 rounded-md px-2 py-2 text-left text-xs transition-colors ${activeView === view ? "bg-athena-accent/15 text-athena-accent" : "text-athena-muted hover:bg-athena-bg hover:text-athena-text"}`}>
              <Icon className="h-4 w-4 shrink-0" /><span className="truncate">{label}</span>
            </button>
          ))}
        </div>
      </nav>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="flex items-center justify-between px-4 pb-1 pt-3">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-athena-muted">项目</span>
          <button type="button" onClick={onCreateProject} className="rounded p-1 text-athena-muted hover:bg-athena-bg hover:text-athena-text" title="创建项目" aria-label="创建项目"><Plus className="h-4 w-4" /></button>
        </div>
        {projects.length === 0 ? (
          <div className="px-4 py-3 text-sm text-athena-muted">
            <p>先创建一个项目，再开始聊天。</p>
            <button type="button" onClick={onCreateProject} className="mt-2 text-athena-accent hover:underline">创建项目</button>
          </div>
        ) : (
          <ul className="pb-2">
            {projects.map((project) => {
              const projectSessions = sessions.filter((session) => session.project_id === project.id)
              const projectDrafts = drafts.filter((draft) => draft.projectId === project.id)
              const isExpanded = expanded.has(project.id)
              const isActive = activeProjectId === project.id
              return (
                <li key={project.id}>
                  <div className={`group mx-2 flex min-w-0 items-center rounded-lg ${isActive ? "bg-athena-accent/10 text-athena-text" : "text-athena-muted hover:bg-athena-bg hover:text-athena-text"}`}>
                    <button type="button" onClick={() => toggleExpanded(project.id)} className="rounded p-1.5" aria-label={`${isExpanded ? "折叠" : "展开"}项目 ${project.name}`} aria-expanded={isExpanded}>
                      {isExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                    </button>
                    {editingProjectId === project.id ? (
                      <>
                        <input autoFocus value={projectNameDraft} onChange={(event) => setProjectNameDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void saveProjectName(); if (event.key === "Escape") setEditingProjectId(null) }} className="min-w-0 flex-1 bg-transparent px-1 py-2 text-sm outline-none" aria-label="项目名称" />
                        <button type="button" onClick={() => void saveProjectName()} className="rounded p-2 text-athena-success" title="保存项目名称"><Check className="h-3.5 w-3.5" /></button>
                      </>
                    ) : (
                      <>
                        <button type="button" onClick={() => onSelectProject(project.id)} className="flex min-w-0 flex-1 items-center gap-2 py-2 text-left" title={project.name}>
                          <Folder className="h-4 w-4 flex-shrink-0" />
                          <span className="truncate text-sm">{project.name}</span>
                          <span className="text-[10px] text-athena-muted">{projectSessions.length}</span>
                        </button>
                        <button type="button" onClick={() => onNewSession(project.id)} className="rounded p-1.5 text-athena-muted hover:bg-athena-border/60 hover:text-athena-text" title={`在 ${project.name} 下新建聊天`} aria-label={`在 ${project.name} 下新建聊天`}><Plus className="h-3.5 w-3.5" /></button>
                        <button type="button" onClick={() => { setEditingProjectId(project.id); setProjectNameDraft(project.name) }} className="rounded p-1.5 text-athena-muted opacity-0 group-hover:opacity-100 hover:bg-athena-border/60 hover:text-athena-text" title="重命名项目" aria-label={`重命名项目 ${project.name}`}><Pencil className="h-3.5 w-3.5" /></button>
                        <button type="button" onClick={() => onDeleteProject(project.id)} className="rounded p-1.5 text-athena-muted opacity-0 group-hover:opacity-100 hover:bg-athena-danger/20 hover:text-athena-danger" title="删除项目" aria-label={`删除项目 ${project.name}`}><Trash2 className="h-3.5 w-3.5" /></button>
                      </>
                    )}
                  </div>
                  {isExpanded && (
                    projectSessions.length > 0 || projectDrafts.length > 0 ? <ul className="ml-3 border-l border-athena-border/70 py-1">{projectDrafts.map(renderDraft)}{projectSessions.map(renderSession)}</ul> : (
                      <button type="button" onClick={() => onNewSession(project.id)} className="ml-10 my-1 rounded px-2 py-1.5 text-left text-xs text-athena-muted hover:bg-athena-bg hover:text-athena-accent">在此项目中开始第一次聊天</button>
                    )
                  )}
                </li>
              )
            })}
          </ul>
        )}
        {sessions.some((session) => !session.project_id) && (
          <div className="border-t border-athena-border/60">
            <div className="px-4 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-wider text-athena-muted">未归属项目</div>
            <ul className="py-1">{sessions.filter((session) => !session.project_id).map(renderSession)}</ul>
          </div>
        )}
      </div>

      <div className="p-3 border-t border-athena-border text-xs text-athena-muted">
        {sessions.length} 个聊天{completedCount > 0 && ` · ${completedCount} 已完成`}{activeCount > 0 && ` · ${activeCount} 运行中`}
      </div>
    </aside>
  )
}

export default Sidebar
