import { Plus, Trash2, MessageSquare, Sparkles } from "lucide-react"
import type { Session } from "../types"

interface SidebarProps {
  sessions: Session[]
  activeSessionId: string | null
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onDeleteSession: (sessionId: string) => void
}

function Sidebar({
  sessions,
  activeSessionId,
  onSelectSession,
  onNewSession,
  onDeleteSession,
}: SidebarProps) {
  return (
    <aside className="w-64 flex-shrink-0 bg-athena-surface border-r border-athena-border flex flex-col h-full">
      {/* Header */}
      <div className="p-4 border-b border-athena-border">
        <div className="flex items-center gap-2 mb-4">
          <Sparkles className="w-6 h-6 text-athena-accent" />
          <span className="font-semibold text-lg">Athena</span>
        </div>
        <button
          onClick={onNewSession}
          className="w-full btn-primary"
        >
          <Plus className="w-4 h-4" />
          New Chat
        </button>
      </div>

      {/* Session List */}
      <div className="flex-1 overflow-y-auto">
        {sessions.length === 0 ? (
          <div className="p-4 text-center text-athena-muted text-sm">
            No sessions yet
          </div>
        ) : (
          <ul className="py-2">
            {sessions.map((session) => (
              <li key={session.id}>
                <div
                  className={`group flex items-center gap-2 px-3 py-2 mx-2 rounded-lg cursor-pointer transition-colors ${
                    activeSessionId === session.id
                      ? "bg-athena-accent/20 text-athena-text"
                      : "hover:bg-athena-bg text-athena-muted hover:text-athena-text"
                  }`}
                  onClick={() => onSelectSession(session.id)}
                >
                  <MessageSquare className="w-4 h-4 flex-shrink-0" />
                  <span className="flex-1 truncate text-sm">
                    {session.title || "Untitled"}
                  </span>
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
            ))}
          </ul>
        )}
      </div>

      {/* Footer */}
      <div className="p-3 border-t border-athena-border text-xs text-athena-muted">
        <span>{sessions.length} session{sessions.length !== 1 ? "s" : ""}</span>
      </div>
    </aside>
  )
}

export default Sidebar
