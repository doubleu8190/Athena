import type { Message } from '../../stores/chatStore'
import SubtaskCard from './SubtaskCard'

interface MessageBubbleProps {
  message: Message
}

// Simple markdown rendering helper
function renderContent(text: string): string {
  if (!text) return ''
  let html = text
    // Escape HTML
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    // Code blocks
    .replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="bg-bg p-3 rounded-lg overflow-x-auto my-2 text-xs"><code>$2</code></pre>')
    // Inline code
    .replace(/`([^`]+)`/g, '<code class="bg-bg px-1.5 py-0.5 rounded text-xs text-accent">$1</code>')
    // Bold
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // Italic
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    // Links
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" class="text-accent hover:underline">$1</a>')
    // Line breaks
    .replace(/\n/g, '<br/>')

  return html
}

export default function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-4`}>
      <div className={`max-w-[85%] lg:max-w-[70%] ${isUser ? 'order-1' : ''}`}>
        {/* Role label */}
        <div className={`text-xs text-text-muted mb-1 ${isUser ? 'text-right' : 'text-left'}`}>
          {isUser ? 'You' : 'Athena'}
        </div>

        {/* Content bubble */}
        <div
          className={`px-4 py-3 rounded-2xl text-sm leading-relaxed ${
            isUser
              ? 'bg-accent text-white rounded-br-md'
              : 'bg-bg-surface border border-border text-text-primary rounded-bl-md'
          }`}
        >
          {message.content ? (
            <div dangerouslySetInnerHTML={{ __html: renderContent(message.content) }} />
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
