import { useEffect, useRef } from 'react'
import type { Message } from '../../stores/chatStore'
import { useChatStore } from '../../stores/chatStore'
import MessageBubble from './MessageBubble'
import SubtaskCard from './SubtaskCard'
import MarkdownRenderer from './MarkdownRenderer'
import ConfirmModal from './ConfirmModal'
import { confirmSubtask } from '../../api/endpoints/chat'

interface Props {
  messages: Message[]
  sessionId: string
}

export default function MessageList({ messages, sessionId }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const updateMessage = useChatStore((s) => s.updateMessage)

  // Auto-scroll to bottom when new messages arrive or streaming
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    // Only auto-scroll if user is near the bottom
    const isNearBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight < 120

    if (isNearBottom) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  const handleConfirm = async (
    taskId: string,
    step: number,
    approved: boolean,
    msgId: string
  ) => {
    try {
      await confirmSubtask({ task_id: taskId, step, approved })
      updateMessage(sessionId, msgId, { confirmRequired: undefined })
    } catch {
      // Keep confirm UI on error — user can retry
    }
  }

  return (
    <div ref={containerRef} className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
      {messages.map((msg) => (
        <div key={msg.id}>
          {msg.role === 'user' ? (
            <MessageBubble content={msg.content} />
          ) : (
            <div className="flex gap-3 max-w-[85%]">
              <span className="text-lg shrink-0 mt-0.5">🦉</span>
              <div className="space-y-3 min-w-0 flex-1">
                {/* Plan indicator */}
                {msg.taskStatus === 'generating_plan' && (
                  <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
                    <span className="w-1.5 h-1.5 bg-orange-500 rounded-full animate-pulse" />
                    Generating plan...
                  </div>
                )}

                {/* Subtask cards */}
                {msg.subtasks && msg.subtasks.length > 0 && (
                  <div className="space-y-2">
                    {msg.subtasks.map((st, i) => (
                      <SubtaskCard key={`${st.step}-${i}`} subtask={st} />
                    ))}
                  </div>
                )}

                {/* Markdown content */}
                {msg.content && (
                  <div className="bg-white dark:bg-gray-900 rounded-xl p-4 border border-gray-200 dark:border-gray-800 shadow-sm">
                    <MarkdownRenderer content={msg.content} />
                  </div>
                )}

                {/* Confirmation required */}
                {msg.confirmRequired && (
                  <ConfirmModal
                    confirm={msg.confirmRequired}
                    onApprove={() =>
                      handleConfirm(msg.confirmRequired!.task_id, msg.confirmRequired!.step, true, msg.id)
                    }
                    onDeny={() =>
                      handleConfirm(msg.confirmRequired!.task_id, msg.confirmRequired!.step, false, msg.id)
                    }
                  />
                )}

                {/* Streaming cursor */}
                {msg.isStreaming && !msg.subtasks?.length && !msg.content && (
                  <div className="flex items-center gap-2">
                    <span className="inline-block w-1.5 h-4 bg-orange-500 rounded-sm animate-pulse" />
                  </div>
                )}

                {/* Task status indicator */}
                {msg.taskStatus && !msg.isStreaming && (
                  <div
                    className={`text-xs font-medium px-2 py-0.5 inline-block rounded ${
                      msg.taskStatus === 'completed'
                        ? 'text-green-600 dark:text-green-400 bg-green-50 dark:bg-green-950'
                        : msg.taskStatus === 'failed'
                          ? 'text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950'
                          : ''
                    }`}
                  >
                    {msg.taskStatus === 'completed'
                      ? '✓ Task completed'
                      : msg.taskStatus === 'failed'
                        ? '✗ Task failed'
                        : ''}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
