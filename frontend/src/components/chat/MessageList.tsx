import { useEffect, useRef } from 'react'
import { useChatStore } from '../../stores/chatStore'
import MessageBubble from './MessageBubble'
import EmptyState from '../shared/EmptyState'

export default function MessageList() {
  const messages = useChatStore((s) => s.messages)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <EmptyState
          icon="💬"
          title="Start a Conversation"
          description="Send a message to begin. Athena will plan and execute tasks, streaming progress in real time."
        />
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-6">
      <div className="max-w-4xl mx-auto">
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
