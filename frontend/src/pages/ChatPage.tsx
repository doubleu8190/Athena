import { useEffect, useRef } from 'react'
import SessionList from '../components/chat/SessionList'
import MessageList from '../components/chat/MessageList'
import ChatInput from '../components/chat/ChatInput'
import { useChatStore, type Message } from '../stores/chatStore'
import { useSSE } from '../hooks/useSSE'

// Stable reference — prevents infinite re-renders caused by
// `useSyncExternalStore` detecting a new `[]` reference on every render.
const EMPTY_MESSAGES: Message[] = []

export default function ChatPage() {
  const activeSessionId = useChatStore((s) => s.activeSessionId)
  const historyLoading = useChatStore((s) => s.historyLoading)

  // Derive messages entirely from store state; use stable EMPTY_MESSAGES
  // so Object.is(prev, next) stays true when there are no messages.
  const messages = useChatStore((s) => {
    const id = s.activeSessionId
    if (!id) return EMPTY_MESSAGES
    return s.messages[id] ?? EMPTY_MESSAGES
  })
  const { isStreaming, startStream, stopStream, resumeStream } = useSSE()
  const initialized = useRef(false)

  // Restore last session from localStorage on first mount
  useEffect(() => {
    if (!initialized.current) {
      initialized.current = true
      useChatStore.getState().restoreLastSession().then(() => {
        // If no previous session was restored, create a fresh one
        const state = useChatStore.getState()
        if (state.sessions.length === 0 && !state.activeSessionId) {
          state.createSession()
        }
      })
    }
  }, [])

  return (
    <div className="h-full flex">
      <SessionList />
      <div className="flex-1 flex flex-col bg-gray-50 dark:bg-gray-950 min-w-0 min-h-0">
        {!activeSessionId ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <div className="text-5xl mb-3">🦉</div>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Select a session or create a new one
              </p>
            </div>
          </div>
        ) : historyLoading ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <div className="w-5 h-5 border-2 border-orange-500 border-t-transparent rounded-full animate-spin mx-auto mb-2" />
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Loading history...
              </p>
            </div>
          </div>
        ) : (
          <>
            <MessageList messages={messages} sessionId={activeSessionId} onResume={resumeStream} />
            <ChatInput
              onSend={startStream}
              onStop={stopStream}
              isStreaming={isStreaming}
            />
          </>
        )}
      </div>
    </div>
  )
}
