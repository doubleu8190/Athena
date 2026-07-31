import { Bot, User, Wrench } from "lucide-react"
import ReactMarkdown from "react-markdown"
import type { Message } from "../types"
import { ToolCard } from "./ToolCard"
import { useChatStore } from "../store/chatStore"

interface MessageBubbleProps {
  message: Message
  isStreaming?: boolean
}

export function MessageBubble({ message, isStreaming }: MessageBubbleProps) {
  const toolCalls = useChatStore((s) => s.toolCalls)

  const isUser = message.role === "user"
  const isTool = message.role === "tool"
  const isSystem = message.role === "system"

  // 关联的工具调用卡片
  const relatedToolCalls = isUser
    ? []
    : toolCalls.filter((tc) => {
        if (message.tool_call_id) {
          return tc.id === message.tool_call_id
        }
        return false
      })

  if (isSystem) {
    return (
      <div className="flex justify-center py-2">
        <div className="px-3 py-1 rounded-full bg-athena-surface border border-athena-border text-xs text-athena-muted">
          {message.content}
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
        {/* Tool Call Cards */}
        {relatedToolCalls.length > 0 && (
          <div className="flex flex-col gap-2 w-full">
            {relatedToolCalls.map((tc) => (
              <ToolCard key={tc.id} toolCall={tc} />
            ))}
          </div>
        )}

        {/* Message Bubble */}
        <div
          className={`rounded-2xl px-4 py-3 ${
            isUser
              ? "bg-athena-accent text-white rounded-tr-sm"
              : isTool
                ? "bg-yellow-500/10 border border-yellow-500/30 rounded-tl-sm"
                : "bg-athena-surface border border-athena-border rounded-tl-sm"
          }`}
        >
          {isTool ? (
            <ToolMessageContent content={message.content} toolName={message.tool_name} />
          ) : (
            <div
              className={`prose prose-invert max-w-none text-sm ${
                isStreaming ? "typing-cursor" : ""
              }`}
            >
              <ReactMarkdown>{message.content}</ReactMarkdown>
            </div>
          )}
        </div>

        {/* Timestamp */}
        <div
          className={`text-xs text-athena-muted ${
            isUser ? "text-right" : "text-left"
          }`}
        >
          {formatTime(message.timestamp)}
        </div>
      </div>
    </div>
  )
}

function ToolMessageContent({
  content,
  toolName,
}: {
  content: string
  toolName?: string
}) {
  // 尝试解析 JSON 结果
  let parsedContent: unknown = null
  try {
    parsedContent = JSON.parse(content)
  } catch {
    // 非 JSON，直接显示
  }

  return (
    <div className="text-sm">
      {toolName && (
        <div className="flex items-center gap-2 mb-2 text-yellow-400">
          <Wrench className="w-3 h-3" />
          <span className="font-mono text-xs">{toolName}</span>
        </div>
      )}
      {parsedContent !== null ? (
        <pre className="text-xs bg-athena-bg/50 rounded p-2 overflow-x-auto text-athena-text/80">
          {JSON.stringify(parsedContent, null, 2)}
        </pre>
      ) : (
        <span className="text-athena-text/80">{content}</span>
      )}
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
