import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { MarkdownContent } from "./MarkdownContent";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import { fileDisplayName } from "../files/model";
import {
  isSkillTool,
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

function MessagePart({
  part,
  sessionId,
  suppressOutput,
}: {
  part: ConversationMessagePart;
  sessionId: string;
  suppressOutput: boolean;
}) {
  if (part.type === "text") {
    return <MarkdownContent text={part.text} />;
  }
  if (part.type === "file_ref") {
    return (
      <div className="message-file-ref">
        <div>
          <strong>文件引用 · {part.filename}</strong>
          <span>
            {part.mime_type ?? "未知类型"}
            {part.size_bytes === null ? "" : ` · ${formatBytes(part.size_bytes)}`}
          </span>
        </div>
        <SessionFileDownloadButton
          compact
          fileId={part.file_id}
          filename={part.filename}
          sessionId={sessionId}
        />
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
      {suppressOutput ? (
        <p className="trace-empty">
          技能原始结果正文不在普通用户消息中展示；状态、交付文件与安全调用详情见执行轨迹。
        </p>
      ) : part.output ? (
        <pre>{part.output}</pre>
      ) : null}
      {!suppressOutput && part.artifact_id ? (
        <span>产物：{part.artifact_id}</span>
      ) : null}
      {(part.artifacts ?? []).length > 0 ? (
        <ul className="message-artifact-list">
          {part.artifacts?.map((artifact) => (
            <li
              className="message-artifact"
              key={artifact.file_id}
            >
              <span>
                <strong>{fileDisplayName(artifact)}</strong>
                <small>
                  {artifact.filename} · {artifact.mime_type} ·{" "}
                  {formatBytes(artifact.size_bytes)}
                </small>
              </span>
              <SessionFileDownloadButton
                compact
                fileId={artifact.file_id}
                filename={fileDisplayName(artifact)}
                sessionId={sessionId}
              />
            </li>
          ))}
        </ul>
      ) : null}
    </details>
  );
}

function MessageRow({
  message,
  sessionId,
  skillToolCallIds,
}: {
  message: ConversationMessage;
  sessionId: string;
  skillToolCallIds: ReadonlySet<string>;
}) {
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
            sessionId={sessionId}
            suppressOutput={
              part.type === "tool_result" &&
              skillToolCallIds.has(part.tool_call_id)
            }
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
  sessionId: string;
  transientText: string;
  transientUnfinished: boolean;
}

export function ChatMessageList({
  error,
  hasMore,
  isLoading,
  isLoadingMore,
  messages,
  onLoadMore,
  onRetry,
  sessionId,
  transientText,
  transientUnfinished,
}: ChatMessageListProps) {
  const skillToolCallIds = new Set<string>();
  messages.forEach((message) => {
    message.parts.forEach((part) => {
      if (part.type === "tool_call" && isSkillTool(part.name)) {
        skillToolCallIds.add(part.tool_call_id);
      }
    });
  });

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
            <MessageRow
              key={`${message.sequence}-${message.id}`}
              message={message}
              sessionId={sessionId}
              skillToolCallIds={skillToolCallIds}
            />
          ))}
          {transientText ? (
            <article
              aria-label="Athena 的临时流式输出"
              className="chat-message chat-message--assistant chat-message--transient"
            >
              <header className="chat-message-meta">
                <strong>Athena</strong>
                <span>{transientUnfinished ? "未完成回答" : "实时草稿"}</span>
                <span>
                  {transientUnfinished
                    ? "仅保留在当前页面，刷新后不承诺恢复"
                    : "等待快照校正"}
                </span>
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

