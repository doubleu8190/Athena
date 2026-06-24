import { useChatStore } from '../../stores/chatStore'

export default function SessionList() {
  const { sessions, activeSessionId, selectSession, createSession } = useChatStore()

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-border-subtle">
        <button
          onClick={createSession}
          className="w-full px-4 py-2.5 bg-accent text-white rounded-xl text-sm font-medium hover:bg-accent-hover transition-all shadow-soft"
        >
          + New Session
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {sessions.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-text-muted">
            No sessions yet.<br />Start a new conversation.
          </div>
        ) : (
          sessions.map((session) => (
            <button
              key={session.id}
              onClick={() => selectSession(session.id)}
              className={`w-full text-left px-4 py-3 transition-all ${
                session.id === activeSessionId
                  ? 'bg-accent/5 border-l-[3px] border-l-accent'
                  : 'hover:bg-bg-elevated border-l-[3px] border-l-transparent'
              }`}
            >
              <div className="text-sm text-text-primary truncate font-medium">
                {session.title}
              </div>
              <div className="text-xs text-text-muted mt-0.5">
                {new Date(session.lastActive).toLocaleDateString()}
              </div>
            </button>
          ))
        )}
      </div>
    </div>
  )
}
