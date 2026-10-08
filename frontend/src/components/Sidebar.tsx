import { Plus, Trash2, MessageSquare, Sparkles, CheckCircle, X, Pencil, Check } from "lucide-react"
import { useState } from "react"
import type { Session } from "../types"

interface SidebarProps {
  sessions: Session[]
  activeSessionId: string | null
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onDeleteSession: (sessionId: string) => void
  onRenameSession?: (sessionId: string, title: string) => Promise<void>
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
    <aside className={`${mobileOpen ? "flex" : "hidden md:flex"} fixed top-0 bottom-12 left-0 z-30 w-[min(82vw,16rem)] md:static md:inset-auto md:w-64 flex-shrink-0 bg-athena-surface border-r border-athena-border flex-col h-full shadow-2xl md:shadow-none`}>
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

      {/* 会话列表 */}
      <div className="flex-1 overflow-y-auto">
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
                  <div
                    className={`group flex items-center gap-2 px-3 py-2 mx-2 rounded-lg cursor-pointer transition-colors ${
                      isActive
                        ? "bg-athena-accent/20 text-athena-text border border-athena-accent/30"
                        : "hover:bg-athena-bg text-athena-muted hover:text-athena-text"
                    }`}
                    onClick={() => onSelectSession(session.id)}
                  >
                    <MessageSquare className={`w-4 h-4 flex-shrink-0 ${isActive ? "text-athena-accent" : isCompleted ? "text-athena-success" : ""}`} />
                    {editingId === session.id ? <input autoFocus value={draftTitle} onChange={(event) => setDraftTitle(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void saveTitle(); if (event.key === "Escape") setEditingId(null) }} onClick={(event) => event.stopPropagation()} className="min-w-0 flex-1 bg-transparent text-sm outline-none" /> : <span className="flex-1 truncate text-sm">{session.title || "Untitled"}</span>}
                    {/* 会话状态指示器 */}
                    {isRunning && (
                      <span className="flex-shrink-0 w-2 h-2 rounded-full bg-athena-accent animate-pulse" />
                    )}
                    {isCompleted && !isActive && (
                      <CheckCircle className="w-3.5 h-3.5 text-athena-success flex-shrink-0" />
                    )}
                    {onRenameSession && editingId !== session.id ? <button type="button" onClick={(event) => { event.stopPropagation(); setEditingId(session.id); setDraftTitle(session.title) }} className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-athena-border/60" title="重命名" aria-label="重命名"><Pencil className="w-3 h-3" /></button> : null}
                    {editingId === session.id ? <button type="button" onClick={(event) => { event.stopPropagation(); void saveTitle() }} className="p-1 rounded text-athena-success" title="保存标题" aria-label="保存标题"><Check className="w-3 h-3" /></button> : null}
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        onDeleteSession(session.id)
                      }}
                      className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-athena-danger/20 hover:text-athena-danger transition-opacity"
                      title="Delete session"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
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
