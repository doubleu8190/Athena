import {
  AlertCircle,
  FileText,
  Image as ImageIcon,
  Loader2,
  RotateCw,
  Sparkles,
  X,
} from "lucide-react";

import { formatBytes } from "../../shared/lib/format";
import { Tooltip, cn } from "../../shared/ui";

const IMAGE_FILE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp", "gif"]);

/**
 * 判断附件是否为常见图片格式（基于 MIME 类型或扩展名），
 * 供场景化模型选择器在所选模型不支持识图时发出温和提醒。
 */
export function isImageAttachmentFile(
  filename: string,
  mimeType?: string | null,
): boolean {
  if (mimeType && mimeType.trim().toLowerCase().startsWith("image/")) {
    return true;
  }
  const dotIndex = filename.lastIndexOf(".");
  if (dotIndex < 0) {
    return false;
  }
  return IMAGE_FILE_EXTENSIONS.has(
    filename.slice(dotIndex + 1).trim().toLowerCase(),
  );
}

export interface ComposerAttachmentChip {
  id: string;
  filename: string;
  sizeBytes: number | null;
  mimeType?: string | null;
  status: "queued" | "uploading" | "ready" | "failed";
  errorMessage?: string | null;
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

/**
 * 一体化输入器顶部内嵌芯片条：
 * - 统一展示待上传/上传中/已就绪/上传失败（支持重试与移除）附件芯片；
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
        const isImage = isImageAttachmentFile(chip.filename, chip.mimeType);

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
              <span className="truncate font-medium" title={chip.filename}>
                {chip.filename}
              </span>
              <span className="shrink-0 text-[11px] opacity-80">上传中…</span>
            </span>
          );
        }

        if (chip.status === "failed") {
          const filenameNode = (
            <span
              className="max-w-[160px] truncate font-medium"
              title={chip.errorMessage ?? chip.filename}
            >
              {chip.filename}
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
                  aria-label={`重试上传：${chip.filename}`}
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
                    chip.removeAriaLabel ?? `移除待上传文件：${chip.filename}`
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

        const FileIcon = isImage ? ImageIcon : FileText;
        return (
          <span
            className="inline-flex max-w-[240px] items-center gap-1.5 rounded-lg border border-border-default bg-subtle px-2.5 py-1 text-xs text-text-primary"
            data-attachment-status={chip.status}
            key={chip.id}
          >
            <FileIcon
              aria-hidden="true"
              className="h-3.5 w-3.5 shrink-0 text-text-tertiary"
            />
            <span className="truncate font-medium" title={chip.filename}>
              {chip.filename}
            </span>
            {chip.sizeBytes !== null ? (
              <span className="shrink-0 text-[11px] text-text-tertiary">
                {formatBytes(chip.sizeBytes)}
              </span>
            ) : null}
            {chip.onRemove ? (
              <button
                aria-label={
                  chip.removeAriaLabel ?? `移除附件：${chip.filename}`
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
