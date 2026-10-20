import { Plus, Trash2, MessageSquare, Sparkles, CheckCircle, X, Pencil, Check } from "lucide-react"
import { useState } from "react"
import type { AppView, Session } from "../types"
import { NAV_ITEMS } from "./NavRail"

interface SidebarProps {
  sessions: Session[]
  activeSessionId: string | null
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onDeleteSession: (sessionId: string) => void
  onRenameSession?: (sessionId: string, title: string) => Promise<void>
  activeView?: AppView
  onSelectView?: (view: AppView) => void
  mobileOpen?: boolean
  onCloseMobile?: () => void
}

function Sidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
  onRenameSession,
  activeView = "chat",
  onSelectView,
  mobileOpen = true,
  onCloseMobile,
}: SidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draftTitle, setDraftTitle] = useState("")
  const saveTitle = async () => {
    if (!editingId || !draftTitle.trim() || !onRenameSession) return
    try {
      await onRenameSession(editingId, draftTitle.trim())
      setEditingId(null)
    } catch {
      // The application status bar displays the request error.
    }
  }
  const completedCount = sessions.filter((s) => s.status === "completed").length
  const activeCount = sessions.filter((s) => s.status === "running").length

  return (
    <aside className={`${mobileOpen ? "flex" : "hidden md:flex"} chat-workspace-sidebar fixed inset-y-0 left-0 z-30 w-[min(88vw,18rem)] md:static md:inset-auto md:w-[248px] flex-shrink-0 bg-athena-surface border-r border-athena-border flex-col h-full shadow-2xl md:shadow-none`}>
      {/* 侧栏标题 */}
      <div className="p-4 border-b border-athena-border">
        <div className="flex items-center gap-2 mb-4">
          <Sparkles className="w-6 h-6 text-athena-accent" />
          <span className="font-semibold text-lg flex-1">Athena</span>
          <button type="button" onClick={onCloseMobile} className="md:hidden btn-ghost p-1.5" title="关闭会话列表" aria-label="关闭会话列表"><X className="w-4 h-4" /></button>
        </div>
        <button
          onClick={onNewSession}
          className="w-full btn-primary"
        >
          <Plus className="w-4 h-4" />
          New Chat
        </button>
      </div>

      <nav className="border-b border-athena-border px-2 pb-3" aria-label="工作区导航">
        <div className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-athena-muted">工作区</div>
        <div className="grid grid-cols-2 gap-1">
          {NAV_ITEMS.map(({ view, label, icon: Icon }) => (
            <button
              key={view}
              type="button"
              onClick={() => onSelectView?.(view)}
              aria-current={activeView === view ? "page" : undefined}
              className={`flex min-w-0 items-center gap-2 rounded-md px-2 py-2 text-left text-xs transition-colors ${activeView === view ? "bg-athena-accent/15 text-athena-accent" : "text-athena-muted hover:bg-athena-bg hover:text-athena-text"}`}
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="truncate">{label}</span>
            </button>
          ))}
        </div>
      </nav>

      {/* 会话列表 */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="px-4 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-wider text-athena-muted">最近会话</div>
        {sessions.length === 0 ? (
          <div className="p-4 text-center text-athena-muted text-sm">
            No sessions yet
          </div>
        ) : (
          <ul className="py-2">
            {sessions.map((session) => {
              const isActive = activeSessionId === session.id
              const isRunning = session.status === "running"
              const isCompleted = session.status === "completed"
              return (
                <li key={session.id}>
                  <div className={`group mx-2 my-1 flex items-center gap-1 rounded-lg px-1 transition-colors ${
                      isActive
                        ? "bg-athena-accent/20 text-athena-text border border-athena-accent/30"
                        : "hover:bg-athena-bg text-athena-muted hover:text-athena-text"
                    }`}>
                    {editingId === session.id ? (
                      <>
                        <input autoFocus value={draftTitle} onChange={(event) => setDraftTitle(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void saveTitle(); if (event.key === "Escape") setEditingId(null) }} className="min-w-0 flex-1 bg-transparent px-2 py-2 text-sm outline-none" aria-label="会话标题" />
                        <button type="button" onClick={() => void saveTitle()} className="rounded p-2 text-athena-success hover:bg-athena-border/60" title="保存标题" aria-label="保存标题"><Check className="h-3.5 w-3.5" /></button>
                      </>
                    ) : (
                      <>
                        <button type="button" onClick={() => onSelectSession(session.id)} className="flex min-w-0 flex-1 items-center gap-2 px-2 py-2 text-left" aria-current={isActive ? "page" : undefined}>
                          <MessageSquare className={`h-4 w-4 flex-shrink-0 ${isActive ? "text-athena-accent" : isCompleted ? "text-athena-success" : ""}`} />
                          <span className="truncate text-sm">{session.title || "Untitled"}</span>
                          {isRunning && <span className="flex-shrink-0 h-2 w-2 rounded-full bg-athena-accent animate-pulse" />}
                          {isCompleted && !isActive && <CheckCircle className="h-3.5 w-3.5 flex-shrink-0 text-athena-success" />}
                        </button>
                        {onRenameSession && <button type="button" onClick={() => { setEditingId(session.id); setDraftTitle(session.title) }} className="rounded p-2 text-athena-muted opacity-0 transition-opacity hover:bg-athena-border/60 hover:text-athena-text group-hover:opacity-100 group-focus-within:opacity-100" title="重命名" aria-label={`重命名 ${session.title}`}><Pencil className="h-3.5 w-3.5" /></button>}
                        <button type="button" onClick={() => onDeleteSession(session.id)} className="rounded p-2 text-athena-muted opacity-0 transition-opacity hover:bg-athena-danger/20 hover:text-athena-danger group-hover:opacity-100 group-focus-within:opacity-100" title="删除会话" aria-label={`删除 ${session.title}`}><Trash2 className="h-3.5 w-3.5" /></button>
                      </>
                    )}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </div>

      {/* 侧栏底部 */}
      <div className="p-3 border-t border-athena-border text-xs text-athena-muted">
        <span>
          {sessions.length} session{sessions.length !== 1 ? "s" : ""}
          {completedCount > 0 && ` · ${completedCount} completed`}
          {activeCount > 0 && ` · ${activeCount} active`}
        </span>
      </div>
    </aside>
  )
}

export default Sidebar
