import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { MarkdownContent } from "./MarkdownContent";
import {
  messageText,
  type ConversationMessage,
  type ConversationMessagePart,
} from "./model";

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function MessagePart({ part }: { part: ConversationMessagePart }) {
  if (part.type === "text") {
    return <MarkdownContent text={part.text} />;
  }
  if (part.type === "file_ref") {
    return (
      <div className="message-file-ref">
        <strong>文件引用 · {part.filename}</strong>
        <span>
          {part.mime_type ?? "未知类型"}
          {part.size_bytes === null ? "" : ` · ${formatBytes(part.size_bytes)}`}
        </span>
      </div>
    );
  }
  if (part.type === "tool_call") {
    return (
      <div className="message-tool-ref">
        <strong>工具调用 · {part.name}</strong>
        <span>{part.tool_call_id}</span>
      </div>
    );
  }
  return (
    <details className="message-tool-ref">
      <summary>
        工具结果 · {part.status} · {part.tool_call_id}
      </summary>
      {part.output ? <pre>{part.output}</pre> : null}
      {part.artifact_id ? <span>产物：{part.artifact_id}</span> : null}
    </details>
  );
}

function MessageRow({ message }: { message: ConversationMessage }) {
  const isAssistant = message.role === "assistant";
  return (
    <article
      aria-label={`${isAssistant ? "Athena" : "你"}的消息，序号 ${
        message.sequence
      }`}
      className={`chat-message chat-message--${isAssistant ? "assistant" : "user"}`}
    >
      <header className="chat-message-meta">
        <strong>{isAssistant ? "Athena" : "你"}</strong>
        <span>#{message.sequence}</span>
        <span>{formatDateTime(message.created_at)}</span>
        <span>{message.status}</span>
      </header>
      <div className="chat-message-body">
        {message.parts.map((part, index) => (
          <MessagePart
            key={`${part.type}-${index}`}
            part={part}
          />
        ))}
        {message.parts.length === 0 ? (
          <p className="chat-message-empty">该消息没有可展示的内容块。</p>
        ) : null}
      </div>
    </article>
  );
}

interface ChatMessageListProps {
  error: unknown;
  hasMore: boolean;
  isLoading: boolean;
  isLoadingMore: boolean;
  messages: ConversationMessage[];
  onLoadMore: () => void;
  onRetry: () => void;
  transientText: string;
}

export function ChatMessageList({
  error,
  hasMore,
  isLoading,
  isLoadingMore,
  messages,
  onLoadMore,
  onRetry,
  transientText,
}: ChatMessageListProps) {
  if (isLoading && !transientText) {
    return (
      <div className="chat-history-state" role="status">
        正在加载消息历史…
      </div>
    );
  }

  return (
    <section className="chat-message-list" aria-label="消息历史">
      {hasMore ? (
        <button
          className="button-quiet chat-load-earlier"
          disabled={isLoadingMore}
          onClick={onLoadMore}
          type="button"
        >
          {isLoadingMore ? "正在加载更早消息…" : "加载更早消息"}
        </button>
      ) : null}

      {error ? (
        <ErrorBanner
          message="消息历史加载失败，请重试。"
          onRetry={onRetry}
        />
      ) : null}

      {!error && messages.length === 0 && !transientText ? (
        <EmptyState
          compact
          description="发送第一条消息后，这里会展示后端保存的真实对话内容。"
          icon="chat"
          title="还没有消息"
        />
      ) : (
        <div className="chat-messages">
          {messages.map((message) => (
            <MessageRow key={`${message.sequence}-${message.id}`} message={message} />
          ))}
          {transientText ? (
            <article
              aria-label="Athena 的临时流式输出"
              className="chat-message chat-message--assistant chat-message--transient"
            >
              <header className="chat-message-meta">
                <strong>Athena</strong>
                <span>实时草稿</span>
                <span>等待快照校正</span>
              </header>
              <div className="chat-message-body">
                <MarkdownContent text={transientText} />
              </div>
            </article>
          ) : null}
        </div>
      )}
    </section>
  );
}

export function findQuestionText(
  messages: ConversationMessage[],
  questionMessageId: string,
): string | null {
  const question = messages.find((message) => message.id === questionMessageId);
  if (!question) {
    return null;
  }
  const text = messageText(question).trim();
  return text || null;
}

