import { useState, useRef, useEffect, type KeyboardEvent } from 'react'
import { Send, Square } from 'lucide-react'

interface Props {
  onSend: (content: string) => void
  onStop: () => void
  isStreaming: boolean
  disabled?: boolean
}

export default function ChatInput({ onSend, onStop, isStreaming, disabled }: Props) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 160) + 'px'
  }, [value])

  const handleSend = () => {
    const trimmed = value.trim()
    if (!trimmed || isStreaming || disabled) return
    onSend(trimmed)
    setValue('')
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <div className="p-4 bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-800">
      <div className="flex items-end gap-3 max-w-4xl mx-auto">
        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type a message... (Enter to send, Shift+Enter for new line)"
          disabled={disabled}
          className="flex-1 resize-none rounded-xl border border-gray-300 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-orange-500 focus:border-transparent placeholder-gray-400 dark:placeholder-gray-500 text-gray-900 dark:text-gray-100 disabled:opacity-50"
        />
        {isStreaming ? (
          <button
            onClick={onStop}
            className="px-4 py-2.5 bg-red-500 hover:bg-red-600 text-white rounded-xl text-sm font-medium transition-colors flex items-center gap-2 shrink-0"
          >
            <Square size={14} fill="currentColor" />
            Stop
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={!value.trim() || disabled}
            className="px-4 py-2.5 bg-orange-500 hover:bg-orange-600 disabled:bg-gray-300 dark:disabled:bg-gray-700 text-white rounded-xl text-sm font-medium transition-colors flex items-center gap-2 shrink-0 disabled:cursor-not-allowed"
          >
            <Send size={14} />
            Send
          </button>
        )}
      </div>
    </div>
  )
}
