import { useChatStore } from '../../stores/chatStore'

export default function SessionList() {
  const { sessions, activeSessionId, selectSession, createSession } = useChatStore()

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-border">
        <button
          onClick={createSession}
          className="w-full px-4 py-2 bg-accent text-white rounded-lg text-sm font-medium hover:bg-accent-hover transition-colors"
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
              className={`w-full text-left px-4 py-3 border-b border-border-subtle transition-colors hover:bg-bg-elevated ${
                session.id === activeSessionId ? 'bg-bg-elevated border-l-2 border-l-accent' : ''
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
