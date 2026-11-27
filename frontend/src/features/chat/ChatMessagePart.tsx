import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  FileCode2,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  Sparkles,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { fileExtension } from "../../shared/lib/file";
import { formatBytes } from "../../shared/lib/format";
import { Badge, Tooltip, cn } from "../../shared/ui";
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
import type { ConversationMessagePart, FileRefMessagePart } from "./model";

/** 助手消息顶部极简 Athena 几何品牌徽标（24×24）。 */
export function AthenaAvatarMark() {
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

export function ExtractionStatusMicroBadge({
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
export function MessageImageCard({
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

export function MessagePart({
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
export function renderMessageParts({
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
