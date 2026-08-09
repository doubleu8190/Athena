import { Bot, User, Wrench, Clock } from "lucide-react"
import ReactMarkdown from "react-markdown"
import type { Message } from "../types"
import { ToolCard } from "./ToolCard"
import { safeStringify } from "../utils/safeStringify"
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

  const hasText = !!message.content.trim()
  const hasToolCalls = (message.tool_calls?.length ?? 0) > 0

  // 无内容且无工具调用的消息不渲染（空气泡）；
  // 纯工具调用回合（content 为空但 tool_calls 有值）需要展示工具卡片。
  if (!isUser && !isTool && !isSystem && !hasText && !hasToolCalls) {
    return null
  }

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
        {/* Assistant 回合发起/落库的工具调用（纯工具回合 content 为空时也可见） */}
        {!isUser && !isTool && hasToolCalls && (
          <div className="flex flex-col gap-2 w-full">
            {(message.tool_calls ?? []).map((tc) => (
              <ToolCard key={tc.id} toolCall={tc} />
            ))}
          </div>
        )}

        {/* Tool Call Cards */}
        {relatedToolCalls.length > 0 && (
          <div className="flex flex-col gap-2 w-full">
            {relatedToolCalls.map((tc) => (
              <ToolCard key={tc.id} toolCall={tc} />
            ))}
          </div>
        )}

        {/* Message Bubble（assistant 纯工具回合无文本，不渲染空气泡） */}
        {(isUser || isTool || isSystem || hasText) && (
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
              <ToolMessageContent
                content={message.content}
                toolName={
                  (message.metadata?.tool_name as string | undefined) ??
                  message.tool_name
                }
              />
            ) : (
              <div
                className={`prose-custom max-w-none text-sm ${
                  isStreaming ? "typing-cursor" : ""
                }`}
              >
                <ReactMarkdown>{message.content}</ReactMarkdown>
              </div>
            )}
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
        <pre className="text-xs bg-athena-bg/50 rounded p-2 overflow-x-auto overflow-y-auto text-athena-text/80 max-h-[24rem] whitespace-pre-wrap">
          {safeStringify(parsedContent, 50000)}
        </pre>
      ) : (
        <span className="text-athena-text/80 whitespace-pre-wrap">{content}</span>
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
