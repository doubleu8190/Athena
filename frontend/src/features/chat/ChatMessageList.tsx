import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { formatDateTime } from "../../shared/lib/format";
import { EmptyState } from "../../shared/ui/EmptyState";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { MarkdownContent } from "./MarkdownContent";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import { fileDisplayName } from "../files/model";
import {
  isSkillTool,
  messageText,
  type ConversationMessage,
  type ConversationMessagePart,
} from "./model";

const FOLLOW_LATEST_THRESHOLD_PX = 48;

export interface ChatAutoScrollRequest {
  afterSequence?: number;
  key: string;
  messageId?: string;
  text?: string;
}

function scrollNodeToLatest(node: HTMLElement) {
  node.scrollTop = node.scrollHeight;
}

function nodeIsNearLatest(node: HTMLElement): boolean {
  const distanceFromLatest =
    node.scrollHeight - node.scrollTop - node.clientHeight;
  return distanceFromLatest <= FOLLOW_LATEST_THRESHOLD_PX;
}

function findAutoScrollTargetMessage(
  messages: ConversationMessage[],
  request: ChatAutoScrollRequest,
): ConversationMessage | null {
  if (request.messageId) {
    return (
      messages.find((message) => message.id === request.messageId) ?? null
    );
  }
  const afterSequence = request.afterSequence;
  if (afterSequence === undefined) {
    return null;
  }
  const expectedText = request.text?.trim() ?? "";
  if (!expectedText) {
    return null;
  }
  return (
    messages.find(
      (message) =>
        message.role === "user" &&
        message.sequence > afterSequence &&
        messageText(message).trim() === expectedText,
    ) ?? null
  );
}

function revealElementIfNeeded(
  element: HTMLElement | null,
  scrollContainer: HTMLElement,
) {
  if (!element || typeof element.scrollIntoView !== "function") {
    return;
  }
  const elementRect = element.getBoundingClientRect();
  const containerRect = scrollContainer.getBoundingClientRect();
  const viewportHeight =
    window.innerHeight || document.documentElement.clientHeight;
  const visibleTop = Math.max(elementRect.top, containerRect.top, 0);
  const visibleBottom = Math.min(
    elementRect.bottom,
    containerRect.bottom,
    viewportHeight,
  );
  const visibleHeight = Math.max(0, visibleBottom - visibleTop);
  const minimumVisibleHeight = Math.max(
    1,
    Math.min(elementRect.height, 64),
  );
  if (visibleHeight < minimumVisibleHeight) {
    // 只有用户主动发送后的目标定位或“回到最新”才允许调整外层页面；
    // nearest 避免强制居中，也不会移动输入焦点。
    element.scrollIntoView({
      block: "nearest",
      inline: "nearest",
    });
  }
}

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
  rowRef,
  sessionId,
  skillToolCallIds,
}: {
  message: ConversationMessage;
  rowRef?: (node: HTMLElement | null) => void;
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
      ref={rowRef}
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
  autoScrollRequest: ChatAutoScrollRequest | null;
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
  autoScrollRequest,
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
  const scrollContainerRef = useRef<HTMLElement | null>(null);
  const autoScrollTargetRef = useRef<HTMLElement | null>(null);
  const latestContentRef = useRef<HTMLElement | null>(null);
  const hasInitializedScrollRef = useRef(false);
  const followingLatestRef = useRef(true);
  const handledAutoScrollRequestRef = useRef<string | null>(null);
  const [followingLatest, setFollowingLatest] = useState(true);
  const autoScrollTargetMessage = autoScrollRequest
    ? findAutoScrollTargetMessage(messages, autoScrollRequest)
    : null;

  useLayoutEffect(() => {
    const node = scrollContainerRef.current;
    if (
      !node ||
      isLoading ||
      hasInitializedScrollRef.current
    ) {
      return;
    }
    // 首次载入默认落在当前会话最新消息，但只操作消息容器，不变更外层页面位置。
    hasInitializedScrollRef.current = true;
    followingLatestRef.current = true;
    setFollowingLatest(true);
    scrollNodeToLatest(node);
  }, [isLoading]);

  useLayoutEffect(() => {
    const node = scrollContainerRef.current;
    if (
      !node ||
      !autoScrollRequest ||
      handledAutoScrollRequestRef.current === autoScrollRequest.key ||
      !autoScrollTargetMessage
    ) {
      return;
    }
    // HTTP 成功只登记目标消息；必须等目标用户消息真实进入列表后再定位，
    // 避免异步持久化或 SSE 顺序尚未到达时滚到旧末尾并误判成功。
    handledAutoScrollRequestRef.current = autoScrollRequest.key;
    followingLatestRef.current = true;
    setFollowingLatest(true);
    scrollNodeToLatest(node);
    revealElementIfNeeded(autoScrollTargetRef.current, node);
  }, [autoScrollRequest, autoScrollTargetMessage]);

  useLayoutEffect(() => {
    const node = scrollContainerRef.current;
    if (
      !node ||
      !hasInitializedScrollRef.current ||
      !followingLatestRef.current
    ) {
      return;
    }
    // 流式文本和历史消息都只驱动消息容器跟随，输入框与执行轨迹不受影响。
    scrollNodeToLatest(node);
  }, [messages, transientText]);

  useEffect(() => {
    const handleResize = () => {
      const node = scrollContainerRef.current;
      if (!node) {
        return;
      }
      if (followingLatestRef.current) {
        scrollNodeToLatest(node);
        return;
      }
      const nextFollowingLatest = nodeIsNearLatest(node);
      followingLatestRef.current = nextFollowingLatest;
      setFollowingLatest(nextFollowingLatest);
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

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

  const showJumpToLatest =
    !followingLatest && (messages.length > 0 || Boolean(transientText));

  const handleLoadMore = () => {
    // 更早消息由浏览器原生滚动锚定保持当前位置；同时退出跟随，
    // 避免异步分页完成时被误判成新消息并带到底部。
    followingLatestRef.current = false;
    setFollowingLatest(false);
    onLoadMore();
  };

  const handleScroll = () => {
    const node = scrollContainerRef.current;
    if (!node) {
      return;
    }
    const nextFollowingLatest = nodeIsNearLatest(node);
    if (nextFollowingLatest === followingLatestRef.current) {
      return;
    }
    followingLatestRef.current = nextFollowingLatest;
    setFollowingLatest(nextFollowingLatest);
  };

  const handleJumpToLatest = () => {
    const node = scrollContainerRef.current;
    followingLatestRef.current = true;
    setFollowingLatest(true);
    if (node) {
      scrollNodeToLatest(node);
      revealElementIfNeeded(latestContentRef.current, node);
    }
  };

  return (
    <div className="chat-message-list-frame">
      <section
        aria-label="消息历史"
        className="chat-message-list"
        onScroll={handleScroll}
        ref={scrollContainerRef}
        tabIndex={0}
      >
        {hasMore ? (
          <button
            className="button-quiet chat-load-earlier"
            disabled={isLoadingMore}
            onClick={handleLoadMore}
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
            {messages.map((message, index) => {
              const isLatestMessage = index === messages.length - 1;
              const isAutoScrollTarget =
                autoScrollTargetMessage?.id === message.id;
              return (
                <MessageRow
                  key={`${message.sequence}-${message.id}`}
                  message={message}
                  rowRef={(node) => {
                    if (isAutoScrollTarget) {
                      autoScrollTargetRef.current = node;
                    }
                    if (isLatestMessage && !transientText) {
                      latestContentRef.current = node;
                    }
                  }}
                  sessionId={sessionId}
                  skillToolCallIds={skillToolCallIds}
                />
              );
            })}
            {transientText ? (
              <article
                aria-label="Athena 的临时流式输出"
                className="chat-message chat-message--assistant chat-message--transient"
                ref={(node) => {
                  latestContentRef.current = node;
                }}
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

      {showJumpToLatest ? (
        <button
          className="button-secondary chat-jump-latest"
          onClick={handleJumpToLatest}
          type="button"
        >
          <Icon name="arrow-right" size={14} />
          回到最新
        </button>
      ) : null}
    </div>
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

