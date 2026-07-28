import { Plus, Trash2 } from 'lucide-react'
import { useChatStore, type Session } from '../../stores/chatStore'

export default function SessionList() {
  const sessions = useChatStore((s) => s.sessions)
  const activeSessionId = useChatStore((s) => s.activeSessionId)
  const createSession = useChatStore((s) => s.createSession)
  const deleteSession = useChatStore((s) => s.deleteSession)
  const loadHistory = useChatStore((s) => s.loadHistory)

  return (
    <div className="w-56 bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800 flex flex-col shrink-0">
      <div className="p-2 border-b border-gray-200 dark:border-gray-800">
        <button
          onClick={() => createSession()}
          className="w-full py-1.5 px-3 text-xs font-medium bg-orange-500 hover:bg-orange-600 text-white rounded-lg transition-colors flex items-center justify-center gap-1.5"
        >
          <Plus size={13} />
          New Session
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-1.5 space-y-0.5">
        {sessions.map((session) => (
          <SessionItem
            key={session.id}
            session={session}
            isActive={session.id === activeSessionId}
            onSelect={() => {
              // If messages already loaded, just switch; otherwise fetch from backend
              const msgs = useChatStore.getState().messages[session.id]
              if (msgs && msgs.length > 0) {
                useChatStore.getState().setActiveSession(session.id)
              } else {
                loadHistory(session.id)
              }
            }}
            onDelete={() => deleteSession(session.id)}
          />
        ))}
        {sessions.length === 0 && (
          <p className="text-xs text-gray-400 text-center py-8">
            No sessions yet.
            <br />
            Start a new chat!
          </p>
        )}
      </div>
    </div>
  )
}

function SessionItem({
  session,
  isActive,
  onSelect,
  onDelete,
}: {
  session: Session
  isActive: boolean
  onSelect: () => void
  onDelete: () => void
}) {
  const timeAgo = getTimeAgo(session.createdAt)

  return (
    <div
      onClick={onSelect}
      className={`group px-2.5 py-2 rounded-lg cursor-pointer transition-colors ${
        isActive
          ? 'bg-orange-50 dark:bg-orange-950 border border-orange-200 dark:border-orange-800'
          : 'hover:bg-gray-50 dark:hover:bg-gray-800/50 border border-transparent'
      }`}
    >
      <div className="flex items-center justify-between">
        <div className="text-xs font-medium text-gray-900 dark:text-gray-100 truncate flex-1 mr-1">
          {session.title}
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation()
            onDelete()
          }}
          className="opacity-0 group-hover:opacity-100 p-0.5 rounded text-gray-400 hover:text-red-500 transition-all shrink-0"
        >
          <Trash2 size={12} />
        </button>
      </div>
      <div className="text-[10px] text-gray-400 mt-0.5">{timeAgo}</div>
    </div>
  )
}

function getTimeAgo(ts: number): string {
  const diff = Date.now() - ts
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}
