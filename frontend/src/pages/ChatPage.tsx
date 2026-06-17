import { useEffect } from 'react'
import { useChatStore } from '../stores/chatStore'
import { useAuthStore } from '../stores/authStore'
import SessionList from '../components/chat/SessionList'
import MessageList from '../components/chat/MessageList'
import ChatInput from '../components/chat/ChatInput'

export default function ChatPage() {
  const { sendMessage, isStreaming } = useChatStore()
  const { checkAuth } = useAuthStore()

  useEffect(() => {
    checkAuth()
  }, [checkAuth])

  return (
    <div className="flex h-full">
      {/* Session sidebar (desktop) */}
      <div className="hidden lg:block w-64 flex-shrink-0 border-r border-border">
        <SessionList />
      </div>

      {/* Chat area */}
      <div className="flex flex-col flex-1 min-w-0">
        <MessageList />
        <ChatInput onSend={sendMessage} disabled={isStreaming} />
      </div>
    </div>
  )
}
