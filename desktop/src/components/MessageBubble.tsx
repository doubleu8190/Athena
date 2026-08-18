import { Bot, User, Wrench, Clock, Paperclip } from "lucide-react"
import ReactMarkdown from "react-markdown"
import type { Message, ToolCallInvocation } from "../types"
import { ToolResultCard } from "./ToolResultCard"
import { useChatStore } from "../store/chatStore"
import type { ToolDisplayData } from "../utils/toolSummary"

interface MessageBubbleProps {
  message: Message
  isStreaming?: boolean
}

export function MessageBubble({ message, isStreaming }: MessageBubbleProps) {
  const toolCalls = useChatStore((s) => s.toolCalls)
  const messages = useChatStore((s) => s.messages)

  const isUser = message.role === "user"
  const isTool = message.role === "tool"
  const isSystem = message.role === "system"

  const hasText = !!message.content.trim()
  const hasToolCalls = (message.tool_calls?.length ?? 0) > 0

  // 无内容且无工具调用的消息不渲染（空气泡）；
  // 纯工具调用回合（content 为空但 tool_calls 有值）需要展示工具卡片。
  if (!isUser && !isTool && !isSystem && !hasText && !hasToolCalls) {
    return null
  }

  const relatedToolCall = isTool ? findToolCallForToolMessage(message, toolCalls) : null
  const pendingToolInvocations = !isUser && !isTool
    ? (message.tool_calls ?? []).filter((tc) => !hasToolResultMessage(tc, messages))
    : []

  if (!isUser && !isTool && !isSystem && !hasText && hasToolCalls && pendingToolInvocations.length === 0) {
    return null
  }

  if (isSystem) {
    return (
      <div className="flex justify-center py-2">
        <div className="flex items-center gap-2 px-3 py-1 rounded-full bg-athena-surface border border-athena-border">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-athena-accent border border-athena-accent/40 rounded px-1.5 py-0.5 flex-shrink-0">
            System
          </span>
          <span className="text-xs text-athena-muted">{message.content}</span>
        </div>
      </div>
    )
  }

  return (
    <div
      className={`flex gap-3 ${
        isUser ? "flex-row-reverse" : "flex-row"
      } animate-fade-in`}
    >
      {/* Avatar */}
      <div
        className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
          isUser
            ? "bg-athena-accent text-white"
            : isTool
              ? "bg-yellow-500/20 text-yellow-400"
              : "bg-athena-surface text-athena-accent border border-athena-border"
        }`}
      >
        {isUser ? (
          <User className="w-4 h-4" />
        ) : isTool ? (
          <Wrench className="w-4 h-4" />
        ) : (
          <Bot className="w-4 h-4" />
        )}
      </div>

      {/* Content */}
      <div
        className={`flex flex-col gap-2 max-w-[85%] ${
          isUser ? "items-end" : "items-start"
        }`}
      >
        {/* Assistant 发起的工具调用：结果消息未到达前显示 pending，避免完成后重复。 */}
        {!isUser && !isTool && pendingToolInvocations.length > 0 && (
          <div className="flex flex-col gap-2 w-full">
            {pendingToolInvocations.map((tc) => (
              <ToolResultCard key={tc.id} toolCall={tc} />
            ))}
          </div>
        )}

        {isTool && (
          <div className="flex flex-col gap-2 w-full">
            <ToolResultCard
              toolCall={relatedToolCall ?? fallbackToolDisplay(message)}
              output={message.content}
              error={relatedToolCall?.error}
            />
          </div>
        )}

        {(message.attachments?.length ?? 0) > 0 && (
          <div className={`flex flex-wrap gap-1.5 ${isUser ? "justify-end" : "justify-start"}`}>
            {message.attachments?.map((attachment) => (
              <div key={attachment.id} className="inline-flex max-w-[260px] items-center gap-1.5 rounded-md border border-athena-border bg-athena-bg px-2 py-1 text-xs text-athena-muted">
                <Paperclip className="w-3 h-3 flex-shrink-0" />
                <span className="truncate">{attachment.filename}</span>
                <span className="flex-shrink-0">{attachment.status}</span>
              </div>
            ))}
          </div>
        )}

        {/* Message Bubble（assistant 纯工具回合无文本，不渲染空气泡） */}
        {(isUser || isSystem || (!isTool && hasText)) && (
          <div
            className={`rounded-2xl px-4 py-3 ${
              isUser
                ? "bg-athena-accent text-white rounded-tr-sm"
                : "bg-athena-surface border border-athena-border rounded-tl-sm"
            }`}
          >
            <div
              className={`prose-custom max-w-none text-sm ${
                isStreaming ? "typing-cursor" : ""
              }`}
            >
              <ReactMarkdown>{message.content}</ReactMarkdown>
            </div>
          </div>
        )}

        {/* Timestamp */}
        <div
          className={`text-xs text-athena-muted flex items-center gap-1 ${
            isUser ? "text-right justify-end" : "text-left"
          }`}
        >
          <Clock className="w-3 h-3" />
          {formatTime(message.timestamp)}
        </div>
      </div>
    </div>
  )
}

function formatTime(isoString: string): string {
  try {
    const date = new Date(isoString)
    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    })
  } catch {
    return ""
  }
}

function findToolCallForToolMessage(
  message: Message,
  toolCalls: ReturnType<typeof useChatStore.getState>["toolCalls"],
) {
  return toolCalls.find((tc) => {
    if (message.tool_call_record_id && tc.id === message.tool_call_record_id) return true
    if (message.step_id && tc.step_id === message.step_id) return true
    if (message.tool_call_id && tc.id === message.tool_call_id) return true
    return false
  }) ?? null
}

function hasToolResultMessage(invocation: ToolCallInvocation, messages: Message[]): boolean {
  return messages.some((message) => (
    message.role === "tool" &&
    (message.tool_call_id === invocation.id || message.tool_call_record_id === invocation.id)
  ))
}

function fallbackToolDisplay(message: Message): ToolDisplayData {
  return {
    id: message.tool_call_record_id ?? message.tool_call_id ?? message.id,
    name: message.tool_name ?? "tool",
    arguments: {},
    status: "success",
    output: message.content,
  }
}
