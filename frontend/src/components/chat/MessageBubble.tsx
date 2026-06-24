import type { Message } from '../../stores/chatStore'
import SubtaskCard from './SubtaskCard'
import MarkdownRenderer from './MarkdownRenderer'

interface MessageBubbleProps {
  message: Message
}

export default function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-4 animate-fade-in-up`}>
      <div className={`max-w-[85%] lg:max-w-[70%] ${isUser ? 'order-1' : ''}`}>
        {/* Role label */}
        <div className={`text-xs text-text-muted mb-1 ${isUser ? 'text-right' : 'text-left'}`}>
          {isUser ? 'You' : 'Athena'}
        </div>

        {/* Content bubble */}
        <div
          className={`px-4 py-3 text-sm leading-relaxed ${
            isUser
              ? 'bg-accent text-white rounded-3xl rounded-br-xl'
              : 'bg-bg-surface shadow-soft text-text-primary rounded-3xl rounded-bl-xl'
          }`}
        >
          {message.content ? (
            <MarkdownRenderer content={message.content} />
          ) : (
            <div className="flex items-center gap-2 text-text-muted">
              <div className="flex gap-1">
                <span className="w-2 h-2 bg-text-muted rounded-full animate-pulse" style={{ animationDelay: '0ms' }} />
                <span className="w-2 h-2 bg-text-muted rounded-full animate-pulse" style={{ animationDelay: '150ms' }} />
                <span className="w-2 h-2 bg-text-muted rounded-full animate-pulse" style={{ animationDelay: '300ms' }} />
              </div>
              Thinking...
            </div>
          )}
        </div>

        {/* Subtask progress card (only for assistant messages with a task) */}
        {!isUser && message.task && (
          <div className="mt-2">
            <SubtaskCard task={message.task} />
          </div>
        )}

        {/* Timestamp */}
        <div className={`text-xs text-text-muted mt-1 ${isUser ? 'text-right' : 'text-left'}`}>
          {new Date(message.timestamp).toLocaleTimeString()}
        </div>
      </div>
    </div>
  )
}
