import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  FileCode2,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  Paperclip,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { formatBytes, formatDateTime } from "../../shared/lib/format";
import {
  Badge,
  Button,
  EmptyState,
  InlineAlert,
  Sheet,
  SheetBody,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  Skeleton,
  Tooltip,
} from "../../shared/ui";
import {
  extractionErrorSummary,
  fileErrorMessage,
} from "../files/errorMessages";
import { useSessionFiles } from "../files/hooks";
import {
  canInlinePreviewFile,
  fileDisplayName,
  fileExtension,
  isPreviewableImageFile,
  sessionFilePreviewUrl,
  sessionFileToPreviewTarget,
  type FilePreviewTarget,
  type SessionFile,
} from "../files/model";
import { SessionFileDownloadButton } from "../files/SessionFileDownloadButton";

export interface SessionFilesDrawerProps {
  onOpenChange: (open: boolean) => void;
  onPreviewFile?: (target: FilePreviewTarget) => void;
  open: boolean;
  sessionId: string;
}

function DrawerFileThumbnail({
  displayName,
  file,
  onPreview,
  sessionId,
}: {
  displayName: string;
  file: SessionFile;
  onPreview?: () => void;
  sessionId: string;
}) {
  const [imgError, setImgError] = useState(false);
  const isSafeImage = isPreviewableImageFile(file.filename, file.mime_type);
  const previewSrc = isSafeImage
    ? sessionFilePreviewUrl(sessionId, file.id)
    : null;

  useEffect(() => {
    setImgError(false);
  }, [previewSrc]);

  if (isSafeImage && previewSrc && !imgError) {
    const imageNode = (
      <img
        alt={displayName}
        className="h-10 w-10 rounded-lg border border-border-subtle bg-subtle object-cover"
        loading="lazy"
        onError={() => setImgError(true)}
        src={previewSrc}
      />
    );
    if (onPreview) {
      return (
        <button
          aria-label={`查看大图：${displayName}`}
          className="mt-0.5 shrink-0 rounded-lg transition-opacity hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
          data-compact-control="true"
          onClick={onPreview}
          type="button"
        >
          {imageNode}
        </button>
      );
    }
    return <span className="mt-0.5 shrink-0">{imageNode}</span>;
  }

  const ext = fileExtension(file.filename);
  const FallbackIcon = isSafeImage
    ? ImageIcon
    : ext === "csv" || ext === "xlsx"
      ? FileSpreadsheet
      : ext === "md" || ext === "json"
        ? FileCode2
        : FileText;

  return (
    <span
      aria-hidden="true"
      className="mt-0.5 inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-subtle text-text-secondary"
    >
      <FallbackIcon className="h-4 w-4" />
    </span>
  );
}

function FileRow({
  file,
  onPreviewFile,
  sessionId,
}: {
  file: SessionFile;
  onPreviewFile?: (target: FilePreviewTarget) => void;
  sessionId: string;
}) {
  const displayName = fileDisplayName(file);
  const canPreview = Boolean(
    onPreviewFile && canInlinePreviewFile(file.filename, file.mime_type),
  );
  const triggerPreview =
    canPreview && onPreviewFile
      ? () => onPreviewFile(sessionFileToPreviewTarget(file))
      : undefined;

  const showExtractionBadge =
    file.extraction_status === "ready" || file.extraction_status === "failed";

  const readyTooltip =
    file.extracted_text_bytes !== null &&
    file.extracted_text_bytes !== undefined
      ? `已完成结构化文本提取（${formatBytes(file.extracted_text_bytes)}）`
      : "已完成结构化文本提取，支持在线预览与对话阅读";

  return (
    <li className="flex flex-wrap items-start justify-between gap-2.5 rounded-xl border border-border-subtle bg-canvas p-3 transition-colors hover:border-border-default">
      <div className="flex min-w-0 flex-1 items-start gap-2.5">
        <DrawerFileThumbnail
          displayName={displayName}
          file={file}
          onPreview={triggerPreview}
          sessionId={sessionId}
        />
        <div className="min-w-0 flex-1 space-y-0.5">
          <div className="flex flex-wrap items-center gap-1.5">
            <p
              className="min-w-0 truncate text-sm font-medium text-text-primary"
              title={displayName}
            >
              {displayName}
            </p>
            {showExtractionBadge && file.extraction_status === "ready" ? (
              <Tooltip content={readyTooltip}>
                <span
                  className="inline-flex shrink-0 cursor-help items-center gap-0.5 rounded-md border border-success-border bg-success-bg px-1.5 py-0.5 text-[10px] leading-none font-medium text-success focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus"
                  tabIndex={0}
                >
                  <CheckCircle2 aria-hidden="true" className="h-2.5 w-2.5" />
                  <span>已解析</span>
                </span>
              </Tooltip>
            ) : null}
            {showExtractionBadge && file.extraction_status === "failed" ? (
              <Tooltip content={extractionErrorSummary(file.extraction_error_code)}>
                <span
                  className="inline-flex shrink-0 cursor-help items-center gap-0.5 rounded-md border border-warning-border bg-warning-bg px-1.5 py-0.5 text-[10px] leading-none font-medium text-warning focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring-focus"
                  tabIndex={0}
                >
                  <AlertTriangle aria-hidden="true" className="h-2.5 w-2.5" />
                  <span>未提取</span>
                </span>
              </Tooltip>
            ) : null}
          </div>
          {displayName !== file.filename ? (
            <p className="truncate text-xs text-text-tertiary">
              {file.filename}
            </p>
          ) : null}
          <p className="text-[11px] text-text-tertiary">
            {formatBytes(file.size_bytes)} · {formatDateTime(file.created_at)}
          </p>
        </div>
      </div>

      <div className="flex shrink-0 flex-wrap items-center gap-2 self-center">
        {triggerPreview ? (
          <button
            aria-label={`预览文件：${displayName}`}
            className="inline-flex items-center gap-1 rounded-md border border-border-default bg-canvas px-2 py-1 text-xs font-medium text-text-primary transition-colors hover:border-brand-border hover:bg-brand-subtle hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus"
            data-compact-control="true"
            onClick={triggerPreview}
            type="button"
          >
            <Eye aria-hidden="true" className="h-3 w-3" />
            <span>预览</span>
          </button>
        ) : null}
        <SessionFileDownloadButton
          compact
          fileId={file.id}
          filename={displayName}
          sessionId={sessionId}
        />
      </div>
    </li>
  );
}

/**
 * 右侧会话文件抽屉（Radix Sheet）：
 * 集中展示本会话的全部「上传附件」与「生成产物」，提供缩略图、解析状态微标、在线预览与一键下载。
 */
export function SessionFilesDrawer({
  onOpenChange,
  onPreviewFile,
  open,
  sessionId,
}: SessionFilesDrawerProps) {
  const filesQuery = useSessionFiles(sessionId, Boolean(sessionId));

  const uniqueFiles = useMemo(() => {
    const flattened = (filesQuery.data?.pages ?? []).flatMap(
      (page) => page.items,
    );
    return Array.from(
      new Map(flattened.map((file) => [file.id, file])).values(),
    );
  }, [filesQuery.data?.pages]);

  const uploadedFiles = useMemo(
    () => uniqueFiles.filter((file) => file.source === "upload"),
    [uniqueFiles],
  );
  const artifactFiles = useMemo(
    () => uniqueFiles.filter((file) => file.source === "artifact"),
    [uniqueFiles],
  );

  return (
    <Sheet onOpenChange={onOpenChange} open={open}>
      <SheetContent side="right">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <span>会话文件</span>
            {uniqueFiles.length > 0 ? (
              <Badge variant="neutral">{uniqueFiles.length}</Badge>
            ) : null}
          </SheetTitle>
          <SheetDescription>
            集中查看并下载本对话上传的附件与助手生成的交付文件。
          </SheetDescription>
        </SheetHeader>

        <SheetBody className="space-y-5">
          {filesQuery.isPending ? (
            <div aria-label="正在加载会话文件" className="space-y-3" role="status">
              <Skeleton className="h-16 w-full rounded-xl" />
              <Skeleton className="h-16 w-full rounded-xl" />
              <Skeleton className="h-16 w-full rounded-xl" />
            </div>
          ) : filesQuery.isError ? (
            <InlineAlert
              action={
                <Button
                  onClick={() => void filesQuery.refetch()}
                  size="xs"
                  variant="outline"
                >
                  <RefreshCw aria-hidden="true" className="h-3 w-3" />
                  重试
                </Button>
              }
              description={fileErrorMessage(filesQuery.error)}
              title="会话文件加载失败"
              variant="danger"
            />
          ) : uniqueFiles.length === 0 ? (
            <EmptyState
              compact
              description="上传的参考资料和助手生成的报表、文档都会自动归档到这里。"
              icon="folder"
              title="暂无会话文件"
            />
          ) : (
            <>
              {artifactFiles.length > 0 ? (
                <section aria-label="生成产物" className="space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
                      <Sparkles
                        aria-hidden="true"
                        className="h-3.5 w-3.5 text-brand"
                      />
                      <span>生成产物</span>
                    </h3>
                    <span className="text-xs text-text-tertiary">
                      {artifactFiles.length} 个
                    </span>
                  </div>
                  <ul className="space-y-2">
                    {artifactFiles.map((file) => (
                      <FileRow
                        file={file}
                        key={file.id}
                        onPreviewFile={onPreviewFile}
                        sessionId={sessionId}
                      />
                    ))}
                  </ul>
                </section>
              ) : null}

              {uploadedFiles.length > 0 ? (
                <section aria-label="上传附件" className="space-y-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
                      <Paperclip
                        aria-hidden="true"
                        className="h-3.5 w-3.5 text-text-tertiary"
                      />
                      <span>上传附件</span>
                    </h3>
                    <span className="text-xs text-text-tertiary">
                      {uploadedFiles.length} 个
                    </span>
                  </div>
                  <ul className="space-y-2">
                    {uploadedFiles.map((file) => (
                      <FileRow
                        file={file}
                        key={file.id}
                        onPreviewFile={onPreviewFile}
                        sessionId={sessionId}
                      />
                    ))}
                  </ul>
                </section>
              ) : null}

              {filesQuery.hasNextPage ? (
                <Button
                  className="w-full"
                  disabled={filesQuery.isFetchingNextPage}
                  onClick={() => void filesQuery.fetchNextPage()}
                  size="sm"
                  variant="outline"
                >
                  {filesQuery.isFetchingNextPage
                    ? "正在加载更多…"
                    : "加载更多文件"}
                </Button>
              ) : null}
            </>
          )}
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}
