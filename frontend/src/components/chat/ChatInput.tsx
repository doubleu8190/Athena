import { useState, useRef, useCallback, type KeyboardEvent } from 'react'
import { Send, Square } from 'lucide-react'

interface ChatInputProps {
  onSend: (content: string) => void
  onStop?: () => void
  disabled?: boolean
  isStreaming?: boolean
}

export default function ChatInput({ onSend, onStop, disabled, isStreaming }: ChatInputProps) {
  const [content, setContent] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const handleSend = useCallback(() => {
    const trimmed = content.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setContent('')
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [content, disabled, onSend])

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleInput = () => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 200) + 'px'
  }

  return (
    <div className="bg-bg-surface shadow-soft px-4 py-3">
      <div className="flex items-end gap-3 max-w-4xl mx-auto">
        <textarea
          ref={textareaRef}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          placeholder="Type a message... (Ctrl+Enter to send)"
          rows={1}
          disabled={disabled}
          className="flex-1 px-4 py-2.5 bg-bg border border-border rounded-2xl text-sm text-text-primary placeholder:text-text-muted resize-none focus:outline-none focus:border-accent focus:ring-2 focus:ring-accent/20 transition-all disabled:opacity-50"
        />
        {isStreaming && onStop ? (
          <button
            onClick={onStop}
            className="flex-shrink-0 p-2.5 bg-error text-white rounded-full text-sm font-medium hover:opacity-90 transition-all shadow-soft"
            aria-label="Stop streaming"
          >
            <Square size={16} fill="currentColor" />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={disabled || !content.trim()}
            className="flex-shrink-0 px-5 py-2.5 bg-gradient-to-r from-accent to-accent-hover text-white rounded-full text-sm font-medium hover:opacity-90 transition-all disabled:opacity-40 disabled:cursor-not-allowed shadow-soft"
            aria-label="Send message"
          >
            <Send size={16} />
          </button>
        )}
      </div>
      {isStreaming && (
        <p className="text-center text-xs text-text-muted mt-2 animate-pulse">Streaming response...</p>
      )}
    </div>
  )
}
