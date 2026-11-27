import { ArrowDown, RefreshCw } from "lucide-react";
import {
  Fragment,
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { copyTextToClipboard } from "../../shared/lib/clipboard";
import {
  Button,
  EmptyState,
  InlineAlert,
  Skeleton,
  toast,
} from "../../shared/ui";
import type { FilePreviewTarget, SessionFile } from "../files/model";
import { ChatMessageRow, TransientMessageRow } from "./ChatMessageRow";
import {
  isSkillTool,
  messageText,
  type ConversationMessage,
  type RunStatus,
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
    // 只有用户主动发送后的目标定位或「回到底部」才允许调整外层位置；
    // nearest 避免强制居中，也不会抢占输入框焦点。
    element.scrollIntoView({
      block: "nearest",
      inline: "nearest",
    });
  }
}

export interface ChatMessageListProps {
  autoScrollRequest: ChatAutoScrollRequest | null;
  /** 置于最新一轮助手回复上方的消息内折叠进度条插槽。 */
  beforeLatestAssistantSlot?: ReactNode;
  currentRunId?: string | null;
  currentRunModelLabel?: string | null;
  currentRunStatus?: RunStatus | null;
  error: unknown;
  /** 置于消息流底部的业务交互卡片插槽（审批卡片、失败卡片、取消说明等）。 */
  footerSlot?: ReactNode;
  hasMore: boolean;
  isLatestRun?: boolean;
  isLoading: boolean;
  isLoadingMore: boolean;
  messages: ConversationMessage[];
  onEditResend?: (text: string) => void;
  onLoadMore: () => void;
  onPreviewFile?: (target: FilePreviewTarget) => void;
  onRetry: () => void;
  onRetryRun?: () => void;
  sessionFilesById?: ReadonlyMap<string, SessionFile>;
  sessionId: string;
  topAlertSlot?: ReactNode;
  transientText: string;
  transientUnfinished: boolean;
}

/**
 * 沉浸式唯一纵向消息滚动区：
 * - 内容水平居中在 max-w-[780px] 阅读列宽内，消除嵌套盒模型与双滚动条；
 * - 智能跟底（<= 48px 阈值）：用户主动上滑浏览历史时暂停跟底，底部居中浮现丸状「回到底部」按钮；
 * - 全量记忆化消息行，确保流式输出与输入区击键对历史消息保持零重渲染隔离。
 */
export const ChatMessageList = memo(function ChatMessageList({
  autoScrollRequest,
  beforeLatestAssistantSlot = null,
  currentRunId = null,
  currentRunModelLabel = null,
  currentRunStatus = null,
  error,
  footerSlot = null,
  hasMore,
  isLatestRun = false,
  isLoading,
  isLoadingMore,
  messages,
  onEditResend,
  onLoadMore,
  onPreviewFile,
  onRetry,
  onRetryRun,
  sessionFilesById,
  sessionId,
  topAlertSlot = null,
  transientText,
  transientUnfinished,
}: ChatMessageListProps) {
  const scrollContainerRef = useRef<HTMLElement | null>(null);
  const contentWrapperRef = useRef<HTMLDivElement | null>(null);
  // 按消息 ID 登记已挂载 DOM 节点，避免为每条历史消息创建内联 ref 回调破坏 React.memo。
  const rowNodesRef = useRef(new Map<string, HTMLElement>());
  const transientNodeRef = useRef<HTMLElement | null>(null);
  const hasInitializedScrollRef = useRef(false);
  const followingLatestRef = useRef(true);
  const handledAutoScrollRequestRef = useRef<string | null>(null);
  const scrollFrameRef = useRef<number | null>(null);
  const [followingLatest, setFollowingLatest] = useState(true);
  const showingSkeleton = isLoading && !transientText;
  const autoScrollTargetMessage = autoScrollRequest
    ? findAutoScrollTargetMessage(messages, autoScrollRequest)
    : null;

  const cancelScheduledScroll = useCallback(() => {
    if (
      scrollFrameRef.current !== null &&
      typeof window !== "undefined" &&
      typeof window.cancelAnimationFrame === "function"
    ) {
      window.cancelAnimationFrame(scrollFrameRef.current);
    }
    scrollFrameRef.current = null;
  }, []);

  // 将内容变更与 ResizeObserver 触发的跟底滚动合并至单一动画帧，避免高频流式 chunk 反复同步回流。
  const scheduleScrollToLatest = useCallback(() => {
    if (
      typeof window === "undefined" ||
      typeof window.requestAnimationFrame !== "function"
    ) {
      // 无 requestAnimationFrame 的测试或受限环境优雅降级为同步滚动。
      const node = scrollContainerRef.current;
      if (
        node &&
        hasInitializedScrollRef.current &&
        followingLatestRef.current
      ) {
        scrollNodeToLatest(node);
      }
      return;
    }
    if (scrollFrameRef.current !== null) {
      return;
    }
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      const node = scrollContainerRef.current;
      if (
        !node ||
        !hasInitializedScrollRef.current ||
        !followingLatestRef.current
      ) {
        return;
      }
      scrollNodeToLatest(node);
    });
  }, []);

  useEffect(() => cancelScheduledScroll, [cancelScheduledScroll]);

  const handleRowNodeChange = useCallback(
    (messageId: string, node: HTMLElement | null) => {
      if (node) {
        rowNodesRef.current.set(messageId, node);
      } else {
        rowNodesRef.current.delete(messageId);
      }
    },
    [],
  );

  const handleCopyText = useCallback((text: string) => {
    void copyTextToClipboard(text).then((ok) => {
      if (ok) {
        toast.success("已复制到剪贴板");
      } else {
        toast.error("当前环境无法自动复制，请手动选中复制");
      }
    });
  }, []);

  // 仅当历史消息数组引用变化时重算技能工具调用 ID 集合，保持 ChatMessageRow props 稳定。
  const skillToolCallIds = useMemo(() => {
    const ids = new Set<string>();
    messages.forEach((message) => {
      message.parts.forEach((part) => {
        if (part.type === "tool_call" && isSkillTool(part.name)) {
          ids.add(part.tool_call_id);
        }
      });
    });
    return ids;
  }, [messages]);

  // 定位当前 Run 的第一条可见助手消息，将消息内折叠进度条精准挂载在其上方。
  const progressAnchorIndex = useMemo(() => {
    if (!beforeLatestAssistantSlot || !currentRunId) {
      return -1;
    }
    return messages.findIndex(
      (message) =>
        message.role === "assistant" &&
        message.run_id === currentRunId &&
        message.parts.some((part) => part.type !== "tool_call"),
    );
  }, [beforeLatestAssistantSlot, currentRunId, messages]);

  // 仅在最近一轮失败/已取消 Run 的最后一条可见助手消息上展示「重新执行」快捷操作。
  const retryableAssistantMessageId = useMemo(() => {
    if (
      !isLatestRun ||
      !currentRunId ||
      (currentRunStatus !== "failed" && currentRunStatus !== "cancelled")
    ) {
      return null;
    }
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const candidate = messages[index];
      if (
        candidate.role === "assistant" &&
        candidate.run_id === currentRunId &&
        candidate.parts.some((part) => part.type !== "tool_call")
      ) {
        return candidate.id;
      }
    }
    return null;
  }, [currentRunId, currentRunStatus, isLatestRun, messages]);

  useLayoutEffect(() => {
    const node = scrollContainerRef.current;
    if (!node || showingSkeleton || hasInitializedScrollRef.current) {
      return;
    }
    // 首次载入默认落在当前会话最新消息，且只滚动消息容器本身。
    hasInitializedScrollRef.current = true;
    followingLatestRef.current = true;
    setFollowingLatest(true);
    scrollNodeToLatest(node);
  }, [showingSkeleton]);

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
    cancelScheduledScroll();
    handledAutoScrollRequestRef.current = autoScrollRequest.key;
    followingLatestRef.current = true;
    setFollowingLatest(true);
    scrollNodeToLatest(node);
    revealElementIfNeeded(
      rowNodesRef.current.get(autoScrollTargetMessage.id) ?? null,
      node,
    );
  }, [autoScrollRequest, autoScrollTargetMessage, cancelScheduledScroll]);

  useLayoutEffect(() => {
    const node = scrollContainerRef.current;
    if (
      !node ||
      !hasInitializedScrollRef.current ||
      !followingLatestRef.current
    ) {
      return;
    }
    // 流式文本、新消息、执行进度条或底部审批/失败卡片挂载更新时，仅在跟底态下按帧合并滚至底部。
    scheduleScrollToLatest();
  }, [
    beforeLatestAssistantSlot,
    footerSlot,
    messages,
    scheduleScrollToLatest,
    transientText,
  ]);

  useEffect(() => {
    const contentNode = contentWrapperRef.current;
    if (
      showingSkeleton ||
      !contentNode ||
      typeof ResizeObserver === "undefined"
    ) {
      return;
    }
    // 跟底态下监听消息内容容器的实际高度变化（如进度条内部异步拉取步骤或审批卡片展开参数），
    // 与上方布局副作用共用同一 rAF 帧，且严格遵守 followingLatestRef 守卫。
    const observer = new ResizeObserver(() => {
      const scrollNode = scrollContainerRef.current;
      if (
        !scrollNode ||
        !hasInitializedScrollRef.current ||
        !followingLatestRef.current
      ) {
        return;
      }
      scheduleScrollToLatest();
    });
    observer.observe(contentNode);
    return () => observer.disconnect();
  }, [scheduleScrollToLatest, showingSkeleton]);

  useEffect(() => {
    const handleResize = () => {
      const node = scrollContainerRef.current;
      if (!node) {
        return;
      }
      if (followingLatestRef.current) {
        scheduleScrollToLatest();
        return;
      }
      const nextFollowingLatest = nodeIsNearLatest(node);
      followingLatestRef.current = nextFollowingLatest;
      setFollowingLatest(nextFollowingLatest);
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [scheduleScrollToLatest]);

  if (showingSkeleton) {
    return (
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div
          className="mx-auto flex w-full max-w-[780px] flex-col gap-5 px-4 py-8 sm:px-6"
          role="status"
        >
          <span className="sr-only">正在加载消息历史…</span>
          <div className="ml-auto w-2/3 max-w-sm space-y-2">
            <Skeleton className="h-12 w-full rounded-2xl" />
          </div>
          <div className="w-full space-y-2.5">
            <div className="flex items-center gap-2">
              <Skeleton className="h-6 w-6 rounded-md" />
              <Skeleton className="h-4 w-20" />
            </div>
            <Skeleton className="ml-8 h-4 w-5/6" />
            <Skeleton className="ml-8 h-4 w-3/4" />
            <Skeleton className="ml-8 h-4 w-2/3" />
          </div>
        </div>
      </div>
    );
  }

  const showJumpToBottom =
    !followingLatest &&
    (messages.length > 0 || Boolean(transientText) || Boolean(footerSlot));

  const handleLoadMore = () => {
    // 更早消息由浏览器原生滚动锚定保持当前位置；同时退出跟底并取消挂起帧，避免加载完毕后被强行拉到底部。
    cancelScheduledScroll();
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
    if (!nextFollowingLatest) {
      cancelScheduledScroll();
    }
    if (nextFollowingLatest === followingLatestRef.current) {
      return;
    }
    followingLatestRef.current = nextFollowingLatest;
    setFollowingLatest(nextFollowingLatest);
  };

  const handleJumpToBottom = () => {
    const node = scrollContainerRef.current;
    cancelScheduledScroll();
    followingLatestRef.current = true;
    setFollowingLatest(true);
    if (node) {
      scrollNodeToLatest(node);
      const lastMessage = messages[messages.length - 1];
      const latestNode = transientText
        ? transientNodeRef.current
        : lastMessage
          ? (rowNodesRef.current.get(lastMessage.id) ?? null)
          : null;
      revealElementIfNeeded(latestNode, node);
    }
  };

  const isEmptyConversation =
    !error &&
    messages.length === 0 &&
    !transientText &&
    !beforeLatestAssistantSlot &&
    !footerSlot;

  return (
    <div
      className="relative flex min-h-0 flex-1 flex-col overflow-hidden"
      style={{ overflowAnchor: "none" }}
    >
      <section
        aria-label="消息历史"
        className="min-h-0 flex-1 overflow-y-auto focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus"
        onScroll={handleScroll}
        ref={scrollContainerRef}
        style={{ overflowAnchor: "auto" }}
        tabIndex={0}
      >
        <div
          className="mx-auto flex w-full max-w-[780px] flex-col gap-5 px-4 py-6 sm:px-6"
          ref={contentWrapperRef}
        >
          {topAlertSlot}

          {hasMore ? (
            <div className="flex justify-center">
              <Button
                disabled={isLoadingMore}
                onClick={handleLoadMore}
                size="xs"
                variant="outline"
              >
                {isLoadingMore ? "正在加载更早消息…" : "加载更早消息"}
              </Button>
            </div>
          ) : null}

          {error ? (
            <InlineAlert
              action={
                <Button onClick={onRetry} size="xs" variant="outline">
                  <RefreshCw aria-hidden="true" className="h-3 w-3" />
                  重试
                </Button>
              }
              description="消息历史加载失败，请重试。"
              variant="danger"
            />
          ) : null}

          {isEmptyConversation ? (
            <div className="py-10">
              <EmptyState
                compact
                description="在下方输入您的问题，Athena 将结合企业知识库与可用工具为您处理。"
                icon="chat"
                title="开始这段对话"
              />
            </div>
          ) : (
            <div className="flex flex-col gap-6">
              {messages.map((message, index) => (
                <Fragment key={`${message.sequence}-${message.id}`}>
                  {index === progressAnchorIndex
                    ? beforeLatestAssistantSlot
                    : null}
                  <ChatMessageRow
                    canRetryMessage={
                      message.id === retryableAssistantMessageId
                    }
                    message={message}
                    modelLabel={
                      message.role === "assistant" &&
                      currentRunId &&
                      message.run_id === currentRunId
                        ? currentRunModelLabel
                        : null
                    }
                    onCopyText={handleCopyText}
                    onEditResend={onEditResend}
                    onNodeChange={handleRowNodeChange}
                    onPreviewFile={onPreviewFile}
                    onRetryRun={onRetryRun}
                    sessionFilesById={sessionFilesById}
                    sessionId={sessionId}
                    skillToolCallIds={skillToolCallIds}
                  />
                </Fragment>
              ))}

              {/* 尚未持久化最新助手回复时，将消息内折叠进度条置于流式输出上方。 */}
              {beforeLatestAssistantSlot && progressAnchorIndex === -1
                ? beforeLatestAssistantSlot
                : null}

              {transientText ? (
                <TransientMessageRow
                  modelLabel={currentRunModelLabel}
                  ref={transientNodeRef}
                  text={transientText}
                  unfinished={transientUnfinished}
                />
              ) : null}
            </div>
          )}

          {footerSlot}
        </div>
      </section>

      {showJumpToBottom ? (
        <button
          className="absolute bottom-4 left-1/2 z-10 inline-flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-border-default bg-canvas/95 px-3.5 py-1.5 text-xs font-medium text-text-primary shadow-md backdrop-blur-xs transition-colors hover:border-brand-border hover:bg-brand-subtle hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
          data-compact-control="true"
          onClick={handleJumpToBottom}
          type="button"
        >
          <ArrowDown aria-hidden="true" className="h-3.5 w-3.5" />
          <span>回到底部</span>
        </button>
      ) : null}
    </div>
  );
});

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
