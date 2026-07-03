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
  const sessions = useChatStore((s) => s.sessions)
  const activeSessionId = useChatStore((s) => s.activeSessionId)

  // Derive messages entirely from store state; use stable EMPTY_MESSAGES
  // so Object.is(prev, next) stays true when there are no messages.
  const messages = useChatStore((s) => {
    const id = s.activeSessionId
    if (!id) return EMPTY_MESSAGES
    return s.messages[id] ?? EMPTY_MESSAGES
  })
  const { isStreaming, startStream, stopStream, resumeStream } = useSSE()
  const initialized = useRef(false)

  // Create initial session if none exists (must be in useEffect, NOT during render)
  useEffect(() => {
    if (!initialized.current && sessions.length === 0 && !activeSessionId) {
      initialized.current = true
      useChatStore.getState().createSession()
    }
  }, [sessions.length, activeSessionId])

  return (
    <div className="h-full flex">
      <SessionList />
      <div className="flex-1 flex flex-col bg-gray-50 dark:bg-gray-950 min-w-0">
        {!activeSessionId ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <div className="text-5xl mb-3">🦉</div>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Select a session or create a new one
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
