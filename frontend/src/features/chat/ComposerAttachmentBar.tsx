import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  FileText,
  Image as ImageIcon,
  Loader2,
  RotateCw,
  Sparkles,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";

import { formatBytes } from "../../shared/lib/format";
import { Tooltip, cn } from "../../shared/ui";
import { extractionErrorSummary } from "../files/errorMessages";
import {
  isPreviewableImageFile,
  type SessionFileExtractionStatus,
} from "../files/model";

const GENERIC_BINARY_MIMES = new Set([
  "application/octet-stream",
  "binary/octet-stream",
]);

/**
 * 判断附件是否为普通图片格式（显式排除 SVG / HTML / XHTML / XML 等脚本风险类型与跨 MIME 伪装），
 * 供输入器图标与非识图模型提醒判断。
 */
export function isImageAttachmentFile(
  filename: string,
  mimeType?: string | null,
): boolean {
  if (isPreviewableImageFile(filename, mimeType)) {
    return true;
  }
  const normalizedMime = mimeType?.split(";")[0]?.trim().toLowerCase() ?? "";
  const dotIndex = filename.lastIndexOf(".");
  const ext =
    dotIndex >= 0 ? filename.slice(dotIndex + 1).trim().toLowerCase() : "";
  if (
    ext === "svg" ||
    ext === "svgz" ||
    ext === "html" ||
    ext === "htm" ||
    ext === "xhtml" ||
    ext === "xml" ||
    normalizedMime === "image/svg+xml" ||
    normalizedMime === "text/html" ||
    normalizedMime === "application/xhtml+xml" ||
    normalizedMime === "application/xml" ||
    normalizedMime === "text/xml"
  ) {
    return false;
  }
  if (normalizedMime && !GENERIC_BINARY_MIMES.has(normalizedMime)) {
    // 明确携带非通用二进制 MIME 时，除已由 isPreviewableImageFile 放行的安全位图外仅允许 image/gif，
    // 禁止如 ("fake.png", "application/pdf") 仅凭扩展名误判为图片。
    return normalizedMime === "image/gif" && (!ext || ext === "gif");
  }
  return ext === "gif";
}

export interface ComposerAttachmentChip {
  id: string;
  /** 原始文件名（带真实扩展名），严格用于安全类型与预览白名单判定。 */
  filename: string;
  /** 可选面向用户的展示名（例如产物 label），未提供时回退为 filename。 */
  displayName?: string | null;
  sizeBytes: number | null;
  mimeType?: string | null;
  status: "queued" | "uploading" | "ready" | "failed";
  errorMessage?: string | null;
  extractionStatus?: SessionFileExtractionStatus;
  extractedTextBytes?: number | null;
  extractionErrorCode?: string | null;
  previewUrl?: string | null;
  onPreview?: () => void;
  onRemove?: () => void;
  onRetry?: () => void;
  removeAriaLabel?: string;
}

export interface ComposerSkillChip {
  id: string;
  name: string;
  unavailable?: boolean;
  onRemove?: () => void;
}

export interface ComposerAttachmentBarProps {
  attachments: readonly ComposerAttachmentChip[];
  disabled?: boolean;
  skills: readonly ComposerSkillChip[];
}

/** 20×20 圆角图片缩略图，加载失败自动回退为静态图标。 */
function ChipThumbnail({
  filename,
  mimeType,
  previewUrl,
}: {
  filename: string;
  mimeType?: string | null;
  previewUrl?: string | null;
}) {
  const [imgError, setImgError] = useState(false);

  useEffect(() => {
    setImgError(false);
  }, [previewUrl]);

  if (previewUrl && !imgError && isPreviewableImageFile(filename, mimeType)) {
    return (
      <img
        alt=""
        aria-hidden="true"
        className="h-5 w-5 shrink-0 rounded object-cover ring-1 ring-border-subtle"
        data-testid="composer-attachment-thumb"
        onError={() => setImgError(true)}
        src={previewUrl}
      />
    );
  }

  const FileIcon = isImageAttachmentFile(filename, mimeType)
    ? ImageIcon
    : FileText;
  return (
    <FileIcon
      aria-hidden="true"
      className="h-3.5 w-3.5 shrink-0 text-text-tertiary"
    />
  );
}

/**
 * 一体化输入器顶部内嵌芯片条：
 * - 统一展示待上传/上传中/已就绪/上传失败（支持重试与移除）附件芯片；
 * - 为图片附件展示 20×20 缩略图，为办公文档展示「已解析 / 未提取」状态微标与温和中文提示；
 * - 已就绪且支持预览的附件点击文件名可直达统一在线预览弹窗（与移除按钮分离，杜绝嵌套 button）；
 * - 同步展示已挂载技能的轻量可移除芯片，替代旧版独立文件面板夹层。
 */
export function ComposerAttachmentBar({
  attachments,
  disabled = false,
  skills,
}: ComposerAttachmentBarProps) {
  if (attachments.length === 0 && skills.length === 0) {
    return null;
  }

  return (
    <div
      aria-label="已附加资料与技能"
      className="flex flex-wrap items-center gap-1.5 border-b border-border-subtle/70 px-3.5 pt-3 pb-2.5"
    >
      {attachments.map((chip) => {
        const displayLabel = chip.displayName?.trim() || chip.filename;

        if (chip.status === "uploading") {
          return (
            <span
              className="inline-flex max-w-[240px] items-center gap-1.5 rounded-lg border border-brand-border bg-brand-subtle/60 px-2.5 py-1 text-xs text-brand"
              data-attachment-status="uploading"
              key={chip.id}
              role="status"
            >
              <Loader2
                aria-hidden="true"
                className="h-3.5 w-3.5 shrink-0 animate-spin motion-reduce:animate-none"
              />
              <span className="truncate font-medium" title={displayLabel}>
                {displayLabel}
              </span>
              <span className="shrink-0 text-[11px] opacity-80">上传中…</span>
            </span>
          );
        }

        if (chip.status === "failed") {
          const filenameNode = (
            <span
              className="max-w-[160px] truncate font-medium"
              title={chip.errorMessage ?? displayLabel}
            >
              {displayLabel}
            </span>
          );
          return (
            <span
              className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-danger-border bg-danger-bg px-2.5 py-1 text-xs text-danger"
              data-attachment-status="failed"
              key={chip.id}
              role="alert"
            >
              <AlertCircle
                aria-hidden="true"
                className="h-3.5 w-3.5 shrink-0"
              />
              {chip.errorMessage ? (
                <Tooltip content={chip.errorMessage}>{filenameNode}</Tooltip>
              ) : (
                filenameNode
              )}
              {chip.errorMessage ? (
                <span className="hidden max-w-[180px] truncate text-[11px] opacity-90 sm:inline">
                  · {chip.errorMessage}
                </span>
              ) : null}
              {chip.onRetry ? (
                <button
                  aria-label={`重试上传：${displayLabel}`}
                  className="inline-flex shrink-0 items-center gap-0.5 rounded px-1 py-0.5 text-[11px] font-semibold text-danger transition-colors hover:bg-danger/10 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus disabled:opacity-50"
                  data-compact-control="true"
                  disabled={disabled}
                  onClick={chip.onRetry}
                  type="button"
                >
                  <RotateCw aria-hidden="true" className="h-3 w-3" />
                  重试
                </button>
              ) : null}
              {chip.onRemove ? (
                <button
                  aria-label={
                    chip.removeAriaLabel ?? `移除待上传文件：${displayLabel}`
                  }
                  className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-danger/80 transition-colors hover:bg-danger/10 hover:text-danger focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus disabled:opacity-50"
                  data-compact-control="true"
                  disabled={disabled}
                  onClick={chip.onRemove}
                  type="button"
                >
                  <X aria-hidden="true" className="h-3 w-3" />
                </button>
              ) : null}
            </span>
          );
        }

        const showExtractionBadge =
          chip.status === "ready" &&
          (chip.extractionStatus === "ready" ||
            chip.extractionStatus === "failed");

        const extractionTooltip =
          chip.extractionStatus === "ready"
            ? chip.extractedTextBytes !== null &&
              chip.extractedTextBytes !== undefined
              ? `已完成结构化文本提取（${formatBytes(chip.extractedTextBytes)}）`
              : "已完成结构化文本提取，可直接在线预览与对话阅读"
            : extractionErrorSummary(chip.extractionErrorCode);

        const contentBody = (
          <>
            <ChipThumbnail
              filename={chip.filename}
              mimeType={chip.mimeType}
              previewUrl={chip.previewUrl}
            />
            <span className="truncate font-medium" title={displayLabel}>
              {displayLabel}
            </span>
            {chip.sizeBytes !== null ? (
              <span className="shrink-0 text-[11px] text-text-tertiary">
                {formatBytes(chip.sizeBytes)}
              </span>
            ) : null}
          </>
        );

        return (
          <span
            className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-border-default bg-subtle px-2 py-1 text-xs text-text-primary sm:max-w-[320px]"
            data-attachment-status={chip.status}
            data-extraction-status={chip.extractionStatus ?? undefined}
            key={chip.id}
          >
            {chip.onPreview && chip.status === "ready" ? (
              <button
                aria-label={`预览附件：${displayLabel}`}
                className="inline-flex min-w-0 flex-1 items-center gap-1.5 rounded px-0.5 text-left transition-colors hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus"
                data-compact-control="true"
                onClick={chip.onPreview}
                type="button"
              >
                {contentBody}
              </button>
            ) : (
              <span className="inline-flex min-w-0 flex-1 items-center gap-1.5 px-0.5">
                {contentBody}
              </span>
            )}

            {showExtractionBadge ? (
              <Tooltip content={extractionTooltip}>
                <span
                  className={cn(
                    "inline-flex shrink-0 cursor-help items-center gap-0.5 rounded-md border px-1.5 py-0.5 text-[10px] leading-none font-medium focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus",
                    chip.extractionStatus === "ready"
                      ? "border-success-border bg-success-bg text-success"
                      : "border-warning-border bg-warning-bg text-warning",
                  )}
                  tabIndex={0}
                >
                  {chip.extractionStatus === "ready" ? (
                    <>
                      <CheckCircle2 aria-hidden="true" className="h-2.5 w-2.5" />
                      <span>已解析</span>
                    </>
                  ) : (
                    <>
                      <AlertTriangle aria-hidden="true" className="h-2.5 w-2.5" />
                      <span>未提取</span>
                    </>
                  )}
                </span>
              </Tooltip>
            ) : null}

            {chip.onRemove ? (
              <button
                aria-label={
                  chip.removeAriaLabel ?? `移除附件：${displayLabel}`
                }
                className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-text-tertiary transition-colors hover:bg-muted hover:text-text-primary focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus disabled:opacity-50"
                data-compact-control="true"
                disabled={disabled}
                onClick={chip.onRemove}
                type="button"
              >
                <X aria-hidden="true" className="h-3 w-3" />
              </button>
            ) : null}
          </span>
        );
      })}

      {skills.map((skill) => (
        <span
          className={cn(
            "inline-flex max-w-[200px] items-center gap-1 rounded-lg border px-2 py-1 text-xs font-medium",
            skill.unavailable
              ? "border-danger-border bg-danger-bg text-danger"
              : "border-brand-border bg-brand-subtle text-brand",
          )}
          data-skill-chip-id={skill.id}
          key={`skill-${skill.id}`}
        >
          <Sparkles aria-hidden="true" className="h-3 w-3 shrink-0" />
          <span className="truncate" title={skill.name}>
            {skill.name}
          </span>
          {skill.onRemove ? (
            <button
              aria-label={`移除技能：${skill.name}`}
              className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded opacity-75 transition-colors hover:bg-black/5 hover:opacity-100 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus disabled:opacity-40"
              data-compact-control="true"
              disabled={disabled}
              onClick={skill.onRemove}
              type="button"
            >
              <X aria-hidden="true" className="h-3 w-3" />
            </button>
          ) : null}
        </span>
      ))}
    </div>
  );
}
