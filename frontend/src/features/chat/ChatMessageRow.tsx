import { Copy, Pencil, RotateCcw } from "lucide-react";
import { forwardRef, memo, useCallback } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { Badge } from "../../shared/ui";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import type { FilePreviewTarget, SessionFile } from "../files/model";
import { AthenaAvatarMark, renderMessageParts } from "./ChatMessagePart";
import { messageText, type ConversationMessage } from "./model";

export interface ChatMessageRowProps {
  canRetryMessage: boolean;
  message: ConversationMessage;
  modelLabel: string | null;
  onCopyText: (text: string) => void;
  onEditResend?: (text: string) => void;
  onNodeChange: (messageId: string, node: HTMLElement | null) => void;
  onPreviewFile?: (target: FilePreviewTarget) => void;
  onRetryRun?: () => void;
  sessionFilesById?: ReadonlyMap<string, SessionFile>;
  sessionId: string;
  skillToolCallIds: ReadonlySet<string>;
}

/**
 * 记忆化单条历史消息行（原 MessageRow），配合稳定节点登记回调，
 * 确保流式输出更新 transientText 或输入区高频击键时已有历史消息零重渲染。
 */
export const ChatMessageRow = memo(function ChatMessageRow({
  canRetryMessage,
  message,
  modelLabel,
  onCopyText,
  onEditResend,
  onNodeChange,
  onPreviewFile,
  onRetryRun,
  sessionFilesById,
  sessionId,
  skillToolCallIds,
}: ChatMessageRowProps) {
  const isAssistant = message.role === "assistant";
  const plainText = messageText(message).trim();
  const formattedTime = formatDateTime(message.created_at);
  const hasRenderableParts = message.parts.some(
    (part) => part.type !== "tool_call",
  );

  const bindNode = useCallback(
    (node: HTMLElement | null) => {
      onNodeChange(message.id, node);
    },
    [message.id, onNodeChange],
  );

  // 纯协议层 tool_call 消息不向业务员工渲染空壳卡片，避免视觉噪音。
  if (!hasRenderableParts && message.parts.length > 0) {
    return null;
  }

  if (!isAssistant) {
    return (
      <article
        aria-label={`你的消息，序号 ${message.sequence}`}
        className="group/message flex flex-col items-end gap-1"
        ref={bindNode}
      >
        <div className="min-w-0 max-w-[88%] rounded-2xl rounded-tr-md bg-[#F1F5F3] px-4 py-3 text-sm text-text-primary sm:max-w-[78%]">
          {renderMessageParts({
            isAssistant: false,
            onPreviewFile,
            parts: message.parts,
            sessionFilesById,
            sessionId,
            skillToolCallIds,
          })}
          {message.parts.length === 0 ? (
            <p className="text-xs text-text-tertiary">该消息没有可展示的内容。</p>
          ) : null}
        </div>

        <div className="flex items-center gap-1 pr-1 text-[11px] text-text-tertiary opacity-0 transition-opacity group-focus-within/message:opacity-100 group-hover/message:opacity-100">
          <time className="mr-1">{formattedTime}</time>
          {plainText ? (
            <button
              aria-label="复制消息"
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-text-secondary transition-colors hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={() => onCopyText(plainText)}
              type="button"
            >
              <Copy aria-hidden="true" className="h-3 w-3" />
              <span>复制</span>
            </button>
          ) : null}
          {plainText && onEditResend ? (
            <button
              aria-label="编辑重发"
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-text-secondary transition-colors hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={() => onEditResend(plainText)}
              type="button"
            >
              <Pencil aria-hidden="true" className="h-3 w-3" />
              <span>编辑重发</span>
            </button>
          ) : null}
        </div>
      </article>
    );
  }

  return (
    <article
      aria-label={`Athena的消息，序号 ${message.sequence}`}
      className="group/message flex w-full flex-col gap-2"
      ref={bindNode}
    >
      <header className="flex flex-wrap items-center gap-2">
        <AthenaAvatarMark />
        <strong className="text-sm font-semibold text-text-primary">
          Athena
        </strong>
        {modelLabel ? <Badge variant="neutral">{modelLabel}</Badge> : null}
        <time className="text-xs text-text-tertiary">{formattedTime}</time>
      </header>

      <div className="min-w-0 pl-8">
        {renderMessageParts({
          isAssistant: true,
          onPreviewFile,
          parts: message.parts,
          sessionFilesById,
          sessionId,
          skillToolCallIds,
        })}
        {message.parts.length === 0 ? (
          <p className="text-xs text-text-tertiary">该消息没有可展示的内容。</p>
        ) : null}

        <div className="mt-2 flex flex-wrap items-center gap-1 opacity-0 transition-opacity group-focus-within/message:opacity-100 group-hover/message:opacity-100">
          {plainText ? (
            <button
              aria-label="复制回答"
              className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-secondary transition-colors hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={() => onCopyText(plainText)}
              type="button"
            >
              <Copy aria-hidden="true" className="h-3.5 w-3.5" />
              <span>复制</span>
            </button>
          ) : null}
          {canRetryMessage && onRetryRun ? (
            <button
              aria-label="重新执行"
              className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-secondary transition-colors hover:bg-subtle hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={onRetryRun}
              type="button"
            >
              <RotateCcw aria-hidden="true" className="h-3.5 w-3.5" />
              <span>重新执行</span>
            </button>
          ) : null}
        </div>
      </div>
    </article>
  );
});

export interface TransientMessageRowProps {
  modelLabel: string | null;
  text: string;
  unfinished: boolean;
}

/** 流式生成中的临时助手消息行，独立承接高频 text_delta 更新以免波及历史消息行。 */
export const TransientMessageRow = memo(
  forwardRef<HTMLElement, TransientMessageRowProps>(
    function TransientMessageRow({ modelLabel, text, unfinished }, ref) {
      return (
        <article
          aria-label="Athena 的临时流式输出"
          className="flex w-full flex-col gap-2"
          ref={ref}
        >
          <header className="flex flex-wrap items-center gap-2">
            <AthenaAvatarMark />
            <strong className="text-sm font-semibold text-text-primary">
              Athena
            </strong>
            {modelLabel ? <Badge variant="neutral">{modelLabel}</Badge> : null}
            <Badge variant={unfinished ? "warning" : "brand"}>
              {unfinished ? "未完成回答" : "正在生成"}
            </Badge>
          </header>
          <div className="pl-8">
            <MarkdownContent text={text} />
            {unfinished ? (
              <p className="mt-2 text-xs text-text-tertiary">
                该未完成内容仅保留在当前页面，刷新后不承诺恢复。
              </p>
            ) : null}
          </div>
        </article>
      );
    },
  ),
);
