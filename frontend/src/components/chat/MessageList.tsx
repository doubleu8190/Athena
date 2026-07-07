import { useEffect, useRef } from 'react'
import type { Message } from '../../stores/chatStore'
import MessageBubble from './MessageBubble'
import SubtaskCard from './SubtaskCard'
import MarkdownRenderer from './MarkdownRenderer'
import ConfirmModal from './ConfirmModal'
import type { ConfirmRequest } from '../../api/endpoints/chat'

interface Props {
  messages: Message[]
  sessionId: string
  /** Resume a paused graph execution via SSE after user confirm/deny. */
  onResume: (req: ConfirmRequest, sessionId: string, msgId: string) => void
}

export default function MessageList({ messages, sessionId, onResume }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

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

  const handleConfirm = (approved: boolean, msgId: string) => {
    onResume({ session_id: sessionId, approved }, sessionId, msgId)
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
                {/* Thinking / generating plan indicator */}
                {(msg.taskStatus === 'thinking' ||
                  msg.taskStatus === 'generating_plan') && (
                  <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
                    <span className="w-1.5 h-1.5 bg-orange-500 rounded-full animate-pulse" />
                    {msg.taskStatus === 'generating_plan'
                      ? 'Generating plan...'
                      : 'Thinking...'}
                  </div>
                )}

                {/* Tool call cards (new agent flow) */}
                {msg.toolCalls && msg.toolCalls.length > 0 && (
                  <div className="space-y-2">
                    {msg.toolCalls.map((tc, i) => (
                      <div
                        key={`${tc.tool_name}-${i}`}
                        className={`rounded-lg border px-3 py-2 text-sm ${
                          tc.status === 'running'
                            ? 'border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950 animate-pulse'
                            : tc.status === 'success'
                              ? 'border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-950'
                              : 'border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950'
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-mono font-medium">
                            🔧 {tc.tool_name}
                          </span>
                          <span
                            className={`text-xs ${
                              tc.status === 'running'
                                ? 'text-blue-600 dark:text-blue-400'
                                : tc.status === 'success'
                                  ? 'text-green-600 dark:text-green-400'
                                  : 'text-red-600 dark:text-red-400'
                            }`}
                          >
                            {tc.status === 'running'
                              ? 'running...'
                              : tc.status === 'success'
                                ? '✓'
                                : '✗'}
                          </span>
                        </div>
                        {tc.args_preview && (
                          <div className="text-xs text-gray-500 dark:text-gray-400 mt-1 truncate">
                            {tc.args_preview}
                          </div>
                        )}
                        {tc.output_preview && tc.status !== 'running' && (
                          <div className="text-xs text-gray-500 dark:text-gray-400 mt-1 truncate font-mono">
                            {tc.output_preview}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* Deprecated: Subtask cards (legacy plan-execute flow) */}
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
                      handleConfirm(true, msg.id)
                    }
                    onDeny={() =>
                      handleConfirm(false, msg.id)
                    }
                  />
                )}

                {/* Streaming cursor — show when waiting for first content */}
                {msg.isStreaming &&
                  !msg.toolCalls?.length &&
                  !msg.subtasks?.length &&
                  !msg.content && (
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
