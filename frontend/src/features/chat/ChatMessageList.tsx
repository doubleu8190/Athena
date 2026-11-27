import {
  AlertTriangle,
  ArrowDown,
  CheckCircle2,
  Copy,
  Eye,
  FileCode2,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  Pencil,
  RefreshCw,
  RotateCcw,
  Sparkles,
} from "lucide-react";
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
import { formatBytes, formatDateTime } from "../../shared/lib/format";
import {
  Badge,
  Button,
  EmptyState,
  InlineAlert,
  Skeleton,
  Tooltip,
  cn,
  toast,
} from "../../shared/ui";
import { fileExtension } from "../../shared/lib/file";
import { MarkdownContent } from "../../shared/ui/MarkdownContent";
import { extractionErrorSummary } from "../files/errorMessages";
import {
  canInlinePreviewFile,
  fileDisplayName,
  isPreviewableImageFile,
  mergeSafeFileMimeType,
  sessionFilePreviewUrl,
  type ArtifactFileRef,
  type FilePreviewTarget,
  type SessionFile,
} from "../files/model";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";
import {
  isSkillTool,
  messageText,
  type ConversationMessage,
  type ConversationMessagePart,
  type FileRefMessagePart,
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

/** 助手消息顶部极简 Athena 几何品牌徽标（24×24）。 */
function AthenaAvatarMark() {
  return (
    <span
      aria-hidden="true"
      className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-brand/15 bg-brand text-brand-fg shadow-xs"
    >
      <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24">
        <path
          d="M12 3.5L20 19.5H16.2L14.3 15.6H9.7L7.8 19.5H4L12 3.5Z"
          fill="currentColor"
        />
        <circle cx="12" cy="12.5" fill="#ebf4f0" r="1.9" />
      </svg>
    </span>
  );
}

function resolveFileIcon(filename: string, mimeType?: string | null) {
  if (isPreviewableImageFile(filename, mimeType)) {
    return ImageIcon;
  }
  const ext = fileExtension(filename);
  if (ext === "csv" || ext === "xlsx") {
    return FileSpreadsheet;
  }
  if (ext === "md" || ext === "json") {
    return FileCode2;
  }
  return FileText;
}

function buildPreviewTargetFromFileRef(
  part: FileRefMessagePart,
  sessionFile?: SessionFile,
): FilePreviewTarget {
  return {
    extractedTextBytes: sessionFile?.extracted_text_bytes ?? null,
    extractionErrorCode: sessionFile?.extraction_error_code ?? null,
    extractionStatus: sessionFile?.extraction_status,
    fileId: part.file_id,
    filename: part.filename,
    label: sessionFile?.label ?? null,
    mimeType: mergeSafeFileMimeType(part.mime_type, sessionFile?.mime_type),
    sizeBytes: part.size_bytes ?? sessionFile?.size_bytes ?? null,
  };
}

function buildPreviewTargetFromArtifact(
  artifact: ArtifactFileRef,
  sessionFile?: SessionFile,
): FilePreviewTarget {
  return {
    extractedTextBytes: sessionFile?.extracted_text_bytes ?? null,
    extractionErrorCode: sessionFile?.extraction_error_code ?? null,
    extractionStatus: sessionFile?.extraction_status,
    fileId: artifact.file_id,
    filename: artifact.filename,
    label: artifact.label,
    mimeType: mergeSafeFileMimeType(artifact.mime_type, sessionFile?.mime_type),
    sizeBytes: artifact.size_bytes,
  };
}

function ExtractionStatusMicroBadge({
  file,
}: {
  file: SessionFile | undefined;
}) {
  if (!file) {
    return null;
  }
  const status = file.extraction_status;
  if (status !== "ready" && status !== "failed") {
    return null;
  }
  if (status === "ready") {
    const tip =
      file.extracted_text_bytes !== null
        ? `已完成结构化文本提取（${formatBytes(file.extracted_text_bytes)}）`
        : "已完成结构化文本提取，支持在线预览与对话阅读";
    return (
      <Tooltip content={tip}>
        <span
          className="inline-flex shrink-0 cursor-help items-center gap-0.5 rounded-md border border-success-border bg-success-bg px-1.5 py-0.5 text-[10px] leading-none font-medium text-success focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus"
          tabIndex={0}
        >
          <CheckCircle2 aria-hidden="true" className="h-2.5 w-2.5" />
          <span>已解析</span>
        </span>
      </Tooltip>
    );
  }
  return (
    <Tooltip content={extractionErrorSummary(file.extraction_error_code)}>
      <span
        className="inline-flex shrink-0 cursor-help items-center gap-0.5 rounded-md border border-warning-border bg-warning-bg px-1.5 py-0.5 text-[10px] leading-none font-medium text-warning focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus"
        tabIndex={0}
      >
        <AlertTriangle aria-hidden="true" className="h-2.5 w-2.5" />
        <span>未提取</span>
      </span>
    </Tooltip>
  );
}

/** 对话内安全白名单位图缩略图卡片：固定高度防布局抖动，点击直达原图灯箱，加载失败优雅降级。 */
function MessageImageCard({
  compact = false,
  displayName,
  fileId,
  filename,
  onPreview,
  sessionId,
  sizeBytes,
}: {
  compact?: boolean;
  displayName: string;
  fileId: string;
  filename: string;
  onPreview?: () => void;
  sessionId: string;
  sizeBytes: number | null;
}) {
  const [imgError, setImgError] = useState(false);
  const previewSrc = sessionFilePreviewUrl(sessionId, fileId);

  useEffect(() => {
    setImgError(false);
  }, [previewSrc]);

  const mediaFrame = imgError ? (
    <div
      className={cn(
        "flex w-full flex-col items-center justify-center gap-1.5 bg-subtle px-3 text-xs text-text-tertiary",
        compact ? "h-36" : "h-44",
      )}
    >
      <ImageIcon aria-hidden="true" className="h-5 w-5" />
      <span>图片缩略图暂不可用</span>
    </div>
  ) : (
    <img
      alt={displayName}
      className={cn(
        "block w-full bg-subtle object-cover transition-transform duration-150 group-hover/imgcard:scale-[1.02]",
        compact ? "h-36" : "max-h-52 min-h-[120px]",
      )}
      loading="lazy"
      onError={() => setImgError(true)}
      src={previewSrc}
    />
  );

  return (
    <div className="min-w-0 overflow-hidden rounded-xl border border-border-default bg-canvas shadow-xs">
      {onPreview ? (
        <button
          aria-label={`查看大图：${displayName}`}
          className="group/imgcard block w-full overflow-hidden text-left focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-ring-focus"
          onClick={onPreview}
          type="button"
        >
          {mediaFrame}
        </button>
      ) : (
        <div className="overflow-hidden">{mediaFrame}</div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border-subtle px-2.5 py-2 text-xs">
        <div className="min-w-0 flex-1">
          <strong
            className="block truncate font-medium text-text-primary"
            title={displayName}
          >
            {displayName}
          </strong>
          {displayName !== filename || sizeBytes !== null ? (
            <span className="block truncate text-[11px] text-text-tertiary">
              {displayName !== filename ? `${filename}` : ""}
              {displayName !== filename && sizeBytes !== null ? " · " : ""}
              {sizeBytes !== null ? formatBytes(sizeBytes) : ""}
            </span>
          ) : null}
        </div>
        <SessionFileDownloadButton
          compact
          fileId={fileId}
          filename={displayName}
          sessionId={sessionId}
        />
      </div>
    </div>
  );
}

function MessagePart({
  isAssistant,
  onPreviewFile,
  part,
  sessionFilesById,
  sessionId,
  suppressOutput,
}: {
  isAssistant: boolean;
  onPreviewFile?: (target: FilePreviewTarget) => void;
  part: ConversationMessagePart;
  sessionFilesById?: ReadonlyMap<string, SessionFile>;
  sessionId: string;
  suppressOutput: boolean;
}) {
  if (part.type === "text") {
    return <MarkdownContent text={part.text} />;
  }

  if (part.type === "file_ref") {
    const matchedFile = sessionFilesById?.get(part.file_id);
    const effectiveMimeType = mergeSafeFileMimeType(
      part.mime_type,
      matchedFile?.mime_type,
    );
    const previewTarget = buildPreviewTargetFromFileRef(part, matchedFile);
    const displayName = fileDisplayName({
      filename: part.filename,
      label: matchedFile?.label ?? null,
    });

    if (isPreviewableImageFile(part.filename, effectiveMimeType)) {
      return (
        <div className="mt-2 w-full max-w-[320px]">
          <MessageImageCard
            displayName={displayName}
            fileId={part.file_id}
            filename={part.filename}
            onPreview={
              onPreviewFile ? () => onPreviewFile(previewTarget) : undefined
            }
            sessionId={sessionId}
            sizeBytes={part.size_bytes}
          />
        </div>
      );
    }

    const FileIcon = resolveFileIcon(part.filename, effectiveMimeType);
    const canPreview = Boolean(
      onPreviewFile && canInlinePreviewFile(part.filename, effectiveMimeType),
    );

    return (
      <div
        className={cn(
          "mt-2 flex flex-wrap items-center justify-between gap-2.5 rounded-xl border px-3 py-2 text-xs",
          isAssistant
            ? "border-border-subtle bg-subtle/70"
            : "border-border-default/80 bg-canvas/90",
        )}
      >
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <FileIcon
            aria-hidden="true"
            className="h-4 w-4 shrink-0 text-text-tertiary"
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <strong className="truncate font-medium text-text-primary">
                {part.filename}
              </strong>
              <ExtractionStatusMicroBadge file={matchedFile} />
            </div>
            <span className="block truncate text-[11px] text-text-tertiary">
              {part.mime_type ?? "附件"}
              {part.size_bytes === null
                ? ""
                : ` · ${formatBytes(part.size_bytes)}`}
            </span>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {canPreview && onPreviewFile ? (
            <button
              aria-label={`预览文件：${part.filename}`}
              className="inline-flex items-center gap-1 rounded-md border border-border-default bg-canvas px-2 py-1 text-xs font-medium text-text-primary transition-colors hover:border-brand-border hover:bg-brand-subtle hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
              data-compact-control="true"
              onClick={() => onPreviewFile(previewTarget)}
              type="button"
            >
              <Eye aria-hidden="true" className="h-3 w-3" />
              <span>预览</span>
            </button>
          ) : null}
          <SessionFileDownloadButton
            compact
            fileId={part.file_id}
            filename={part.filename}
            sessionId={sessionId}
          />
        </div>
      </div>
    );
  }

  if (part.type === "tool_call") {
    // 工具调用过程已收敛至消息内折叠进度条，消息流内绝不暴露原始 arguments 或底层 tool_call_id。
    return null;
  }

  const artifacts = part.artifacts ?? [];
  if (artifacts.length > 0) {
    const imageArtifacts = artifacts.filter((artifact) =>
      isPreviewableImageFile(
        artifact.filename,
        mergeSafeFileMimeType(
          artifact.mime_type,
          sessionFilesById?.get(artifact.file_id)?.mime_type,
        ),
      ),
    );
    const nonImageArtifacts = artifacts.filter(
      (artifact) =>
        !isPreviewableImageFile(
          artifact.filename,
          mergeSafeFileMimeType(
            artifact.mime_type,
            sessionFilesById?.get(artifact.file_id)?.mime_type,
          ),
        ),
    );

    return (
      <section
        aria-label="交付产物"
        className="mt-2.5 space-y-2.5 rounded-xl border border-brand-border/80 bg-brand-subtle/30 p-3"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-xs font-semibold text-brand">
            <Sparkles aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
            <span>交付产物</span>
          </div>
          <Badge className="px-1.5 py-0 text-[11px]" variant="brand">
            {artifacts.length} 个文件
          </Badge>
        </div>

        {imageArtifacts.length > 0 ? (
          <div
            className={cn(
              "grid grid-cols-1 gap-2",
              imageArtifacts.length >= 2 && "sm:grid-cols-2",
            )}
          >
            {imageArtifacts.map((artifact) => {
              const displayName = fileDisplayName(artifact);
              const matchedFile = sessionFilesById?.get(artifact.file_id);
              const target = buildPreviewTargetFromArtifact(
                artifact,
                matchedFile,
              );
              return (
                <MessageImageCard
                  compact={imageArtifacts.length >= 2}
                  displayName={displayName}
                  fileId={artifact.file_id}
                  filename={artifact.filename}
                  key={artifact.file_id}
                  onPreview={
                    onPreviewFile ? () => onPreviewFile(target) : undefined
                  }
                  sessionId={sessionId}
                  sizeBytes={artifact.size_bytes}
                />
              );
            })}
          </div>
        ) : null}

        {nonImageArtifacts.length > 0 ? (
          <ul className="space-y-2">
            {nonImageArtifacts.map((artifact) => {
              const displayName = fileDisplayName(artifact);
              const matchedFile = sessionFilesById?.get(artifact.file_id);
              const effectiveMimeType = mergeSafeFileMimeType(
                artifact.mime_type,
                matchedFile?.mime_type,
              );
              const target = buildPreviewTargetFromArtifact(
                artifact,
                matchedFile,
              );
              const FileIcon = resolveFileIcon(
                artifact.filename,
                effectiveMimeType,
              );
              const canPreview = Boolean(
                onPreviewFile &&
                  canInlinePreviewFile(artifact.filename, effectiveMimeType),
              );

              return (
                <li
                  className="flex flex-wrap items-center justify-between gap-2.5 rounded-xl border border-border-default bg-canvas px-3 py-2.5 text-xs shadow-xs"
                  key={artifact.file_id}
                >
                  <div className="flex min-w-0 flex-1 items-center gap-2.5">
                    <span
                      aria-hidden="true"
                      className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-brand-subtle text-brand"
                    >
                      <FileIcon className="h-4 w-4" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-1.5">
                        <strong className="truncate font-medium text-text-primary">
                          {displayName}
                        </strong>
                        <ExtractionStatusMicroBadge file={matchedFile} />
                      </span>
                      <small className="block truncate text-[11px] text-text-tertiary">
                        {artifact.filename} · {formatBytes(artifact.size_bytes)}
                      </small>
                    </span>
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center gap-2">
                    {canPreview && onPreviewFile ? (
                      <button
                        aria-label={`在线预览：${displayName}`}
                        className="inline-flex items-center gap-1 rounded-md border border-brand-border bg-brand-subtle px-2.5 py-1 text-xs font-medium text-brand transition-colors hover:bg-brand hover:text-brand-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
                        data-compact-control="true"
                        onClick={() => onPreviewFile(target)}
                        type="button"
                      >
                        <Eye aria-hidden="true" className="h-3.5 w-3.5" />
                        <span>在线预览</span>
                      </button>
                    ) : null}
                    <SessionFileDownloadButton
                      compact
                      fileId={artifact.file_id}
                      filename={displayName}
                      sessionId={sessionId}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        ) : null}
      </section>
    );
  }

  // 安全铁律：技能原始输出绝不在普通用户消息流中展示。
  if (suppressOutput) {
    return (
      <p className="text-xs text-text-tertiary">
        已完成技能步骤，安全详情与交付文件请在上方执行进度或会话文件抽屉查看。
      </p>
    );
  }

  return (
    <p className="text-xs text-text-tertiary">
      已完成业务工具操作，详细记录可在上方执行进度中展开查看。
    </p>
  );
}

/**
 * 渲染单条消息内的多内容块，并将多张白名单位图 file_ref 自动聚合为紧凑 2 列缩略图网格，
 * 避免多图纵向堆叠过长同时保证窄屏自动回落单列零横向溢出。
 */
function renderMessageParts({
  isAssistant,
  onPreviewFile,
  parts,
  sessionFilesById,
  sessionId,
  skillToolCallIds,
}: {
  isAssistant: boolean;
  onPreviewFile?: (target: FilePreviewTarget) => void;
  parts: ConversationMessagePart[];
  sessionFilesById?: ReadonlyMap<string, SessionFile>;
  sessionId: string;
  skillToolCallIds: ReadonlySet<string>;
}): ReactNode[] {
  const nodes: ReactNode[] = [];
  let index = 0;

  while (index < parts.length) {
    const part = parts[index];
    const partEffectiveMime =
      part.type === "file_ref"
        ? mergeSafeFileMimeType(
            part.mime_type,
            sessionFilesById?.get(part.file_id)?.mime_type,
          )
        : null;
    if (
      part.type === "file_ref" &&
      isPreviewableImageFile(part.filename, partEffectiveMime)
    ) {
      const imageGroup: FileRefMessagePart[] = [];
      const groupStartIndex = index;
      while (index < parts.length) {
        const candidate = parts[index];
        const candidateEffectiveMime =
          candidate.type === "file_ref"
            ? mergeSafeFileMimeType(
                candidate.mime_type,
                sessionFilesById?.get(candidate.file_id)?.mime_type,
              )
            : null;
        if (
          candidate.type === "file_ref" &&
          isPreviewableImageFile(candidate.filename, candidateEffectiveMime)
        ) {
          imageGroup.push(candidate);
          index += 1;
        } else {
          break;
        }
      }

      if (imageGroup.length >= 2) {
        nodes.push(
          <div
            className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2"
            data-testid="message-image-grid"
            key={`image-grid-${groupStartIndex}`}
          >
            {imageGroup.map((imagePart) => {
              const matchedFile = sessionFilesById?.get(imagePart.file_id);
              const target = buildPreviewTargetFromFileRef(
                imagePart,
                matchedFile,
              );
              const displayName = fileDisplayName({
                filename: imagePart.filename,
                label: matchedFile?.label ?? null,
              });
              return (
                <MessageImageCard
                  compact
                  displayName={displayName}
                  fileId={imagePart.file_id}
                  filename={imagePart.filename}
                  key={imagePart.file_id}
                  onPreview={
                    onPreviewFile ? () => onPreviewFile(target) : undefined
                  }
                  sessionId={sessionId}
                  sizeBytes={imagePart.size_bytes}
                />
              );
            })}
          </div>,
        );
      } else {
        const single = imageGroup[0];
        nodes.push(
          <MessagePart
            isAssistant={isAssistant}
            key={`file_ref-${groupStartIndex}`}
            onPreviewFile={onPreviewFile}
            part={single}
            sessionFilesById={sessionFilesById}
            sessionId={sessionId}
            suppressOutput={false}
          />,
        );
      }
      continue;
    }

    nodes.push(
      <MessagePart
        isAssistant={isAssistant}
        key={`${part.type}-${index}`}
        onPreviewFile={onPreviewFile}
        part={part}
        sessionFilesById={sessionFilesById}
        sessionId={sessionId}
        suppressOutput={
          isAssistant &&
          part.type === "tool_result" &&
          skillToolCallIds.has(part.tool_call_id)
        }
      />,
    );
    index += 1;
  }

  return nodes;
}

interface MessageRowProps {
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

// 记忆化历史消息行并配合稳定节点登记回调，确保流式输出更新 transientText 或输入区高频击键时已有历史消息零重渲染。
const MessageRow = memo(function MessageRow({
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
}: MessageRowProps) {
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

  // 仅当历史消息数组引用变化时重算技能工具调用 ID 集合，保持 MessageRow props 稳定。
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
                  <MessageRow
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
                <article
                  aria-label="Athena 的临时流式输出"
                  className="flex w-full flex-col gap-2"
                  ref={transientNodeRef}
                >
                  <header className="flex flex-wrap items-center gap-2">
                    <AthenaAvatarMark />
                    <strong className="text-sm font-semibold text-text-primary">
                      Athena
                    </strong>
                    {currentRunModelLabel ? (
                      <Badge variant="neutral">{currentRunModelLabel}</Badge>
                    ) : null}
                    <Badge variant={transientUnfinished ? "warning" : "brand"}>
                      {transientUnfinished ? "未完成回答" : "正在生成"}
                    </Badge>
                  </header>
                  <div className="pl-8">
                    <MarkdownContent text={transientText} />
                    {transientUnfinished ? (
                      <p className="mt-2 text-xs text-text-tertiary">
                        该未完成内容仅保留在当前页面，刷新后不承诺恢复。
                      </p>
                    ) : null}
                  </div>
                </article>
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
